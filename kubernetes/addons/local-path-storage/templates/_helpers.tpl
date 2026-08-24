{{- define "local-path-storage.labels" -}}
app.kubernetes.io/name: local-path-storage
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "local-path-storage.validate" -}}
{{- if .Values.enabled -}}
  {{- range $name, $image := dict "provisioner.image" .Values.provisioner.image "helper.image" .Values.helper.image -}}
    {{- if not (regexMatch `^.+@sha256:[a-f0-9]{64}$` $image) -}}
      {{- fail (printf "%s must be pinned by sha256 digest" $name) -}}
    {{- end -}}
  {{- end -}}
  {{- if not (has .Values.storageClass.reclaimPolicy (list "Delete" "Retain")) -}}
    {{- fail "storageClass.reclaimPolicy must be Delete or Retain" -}}
  {{- end -}}
  {{- if not (has .Values.storageClass.volumeBindingMode (list "Immediate" "WaitForFirstConsumer")) -}}
    {{- fail "storageClass.volumeBindingMode must be Immediate or WaitForFirstConsumer" -}}
  {{- end -}}
  {{- if or (empty .Values.namespace) (empty .Values.storageClass.name) (empty .Values.nodePath) -}}
    {{- fail "namespace, storageClass.name, and nodePath are required" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
