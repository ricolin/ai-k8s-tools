{{- define "ai-model-workspace.labels" -}}
app.kubernetes.io/name: ai-model-workspace
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ai-model-workspace.validate" -}}
{{- if .Values.enabled -}}
  {{- if not (has .Values.storage.mode (list "dynamic" "static-local")) -}}
    {{- fail "storage.mode must be dynamic or static-local" -}}
  {{- end -}}
  {{- if not (has .Values.storage.retentionPolicy (list "Keep" "Delete")) -}}
    {{- fail "storage.retentionPolicy must be Keep or Delete" -}}
  {{- end -}}
  {{- if and (eq .Values.storage.mode "dynamic") (empty .Values.storage.existingStorageClass) -}}
    {{- fail "dynamic storage requires storage.existingStorageClass" -}}
  {{- end -}}
  {{- if eq .Values.storage.mode "static-local" -}}
    {{- range $name := list "storageClassName" "persistentVolumeName" "path" "nodeSelectorValue" -}}
      {{- if empty (index $.Values.storage.staticLocal $name) -}}
        {{- fail (printf "static-local storage requires storage.staticLocal.%s" $name) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if and .Values.validation.enabled (not (regexMatch `^.+@sha256:[a-f0-9]{64}$` .Values.validation.image)) -}}
    {{- fail "validation.image must be pinned by sha256 digest" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
