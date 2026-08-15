from pathlib import Path

import pytest

from scripts.azure_deploy import (
    TerraformOutputs,
    build_context,
    load_outputs,
    managed_identity_name,
    resource_scope,
)


def terraform_values() -> dict[str, object]:
    location = "West US 3"
    return {
        "aks_cluster_ids": {
            "value": {
                location: (
                    "/subscriptions/sub-1/resourceGroups/rg-app/providers/"
                    "Microsoft.ContainerService/managedClusters/aks-app"
                )
            }
        },
        "aks_cluster_names": {"value": {location: "aks-app"}},
        "application_insights_connection_strings": {
            "value": {location: "InstrumentationKey=example"}
        },
        "foundry_model_deployment_names": {"value": {location: "gpt-example"}},
        "foundry_openai_base_urls": {
            "value": {location: "https://models.example.test/openai/v1/"}
        },
        "foundry_workload_identity_client_ids": {"value": {location: "client-id"}},
        "foundry_workload_identity_ids": {
            "value": {
                location: (
                    "/subscriptions/sub-1/resourceGroups/rg-identity/providers/"
                    "Microsoft.ManagedIdentity/userAssignedIdentities/mi-foundry"
                )
            }
        },
    }


def test_regional_outputs_require_explicit_location_for_multiple_values() -> None:
    outputs = TerraformOutputs({"value": {"East US": "east", "West US": "west"}})

    assert outputs.regional("value", "East US") == "east"
    with pytest.raises(ValueError, match="Set AZURE_LOCATION_KEY"):
        outputs.regional("value", "North Europe")


def test_azure_resource_identifiers_are_parsed_case_insensitively() -> None:
    resource_id = (
        "/SUBSCRIPTIONS/sub-1/RESOURCEGROUPS/rg-example/providers/"
        "Microsoft.ManagedIdentity/userAssignedIdentities/identity-one"
    )

    assert resource_scope(resource_id) == ("sub-1", "rg-example")
    assert managed_identity_name(resource_id) == ("rg-example", "identity-one")


def test_build_context_uses_one_shared_image_and_runtime_values() -> None:
    context = build_context(
        TerraformOutputs(terraform_values()),
        location_key="West US 3",
        image_tag="0.1.0-abcdef0",
        ghcr_owner="example",
        service_account="sundae",
    )

    assert context.subscription_id == "sub-1"
    assert context.resource_group == "rg-app"
    assert context.cluster_name == "aks-app"
    assert context.identity_resource_group == "rg-identity"
    assert context.identity_name == "mi-foundry"
    assert context.image == "ghcr.io/example/sundae-funday:0.1.0-abcdef0"
    assert context.values["image"] == {
        "registry": "ghcr.io/example",
        "repository": "sundae-funday",
        "tag": "0.1.0-abcdef0",
    }
    assert context.values["config"] == {
        "OPENAI_BASE_URL": "https://models.example.test/openai/v1/",
        "OPENAI_CHAT_MODEL": "gpt-example",
        "OPENAI_AUTH_MODE": "workload_identity",
    }
    assert (
        context.values["secret"]["data"]["APPLICATIONINSIGHTS_CONNECTION_STRING"]
        == "InstrumentationKey=example"
    )
    assert context.values["workloadIdentity"]["clientId"] == "client-id"
    assert context.values["workloadIdentity"]["serviceAccount"]["name"] == "sundae"


def test_load_outputs_reads_wrapped_terraform_json(tmp_path: Path) -> None:
    path = tmp_path / "outputs.json"
    path.write_text('{"name":{"value":"expected"}}')

    outputs = load_outputs(path, None)

    assert outputs.output("name") == "expected"
