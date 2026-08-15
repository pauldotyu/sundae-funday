{{- define "sundae-funday.version" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}

{{- define "sundae-funday.image" -}}
{{- $name := .Values.image.repository -}}
{{- if .Values.image.registry -}}
{{- $name = printf "%s/%s" .Values.image.registry $name -}}
{{- end -}}
{{- printf "%s:%s" $name (include "sundae-funday.version" .) -}}
{{- end -}}

{{- define "sundae-funday.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: sundae-funday
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "sundae-funday.secretName" -}}
{{- default .Values.secret.name .Values.secret.existingSecret -}}
{{- end -}}

{{- define "sundae-funday.serviceAccountName" -}}
{{- .Values.workloadIdentity.serviceAccount.name -}}
{{- end -}}
