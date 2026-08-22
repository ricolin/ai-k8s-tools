{{- define "nvidia-ai-readiness.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nvidia-ai-readiness.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "nvidia-ai-readiness.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nvidia-ai-readiness.labels" -}}
app.kubernetes.io/name: {{ include "nvidia-ai-readiness.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "nvidia-ai-readiness.validate" -}}
{{- $gpuOperator := index .Values "gpu-operator" -}}
{{- if .Values.profile.enabled -}}
  {{- if not $gpuOperator.enabled -}}
    {{- fail "profile.enabled requires gpu-operator.enabled=true" -}}
  {{- end -}}
  {{- if not .Values.readiness.enabled -}}
    {{- fail "profile.enabled requires readiness.enabled=true" -}}
  {{- end -}}
{{- end -}}
{{- if .Values.readiness.enabled -}}
  {{- if not (regexMatch `^.+@sha256:[a-f0-9]{64}$` .Values.readiness.orchestratorImage) -}}
    {{- fail "readiness.orchestratorImage must be pinned by sha256 digest" -}}
  {{- end -}}
  {{- if not (regexMatch `^.+@sha256:[a-f0-9]{64}$` .Values.readiness.cudaSmokeImage) -}}
    {{- fail "readiness.cudaSmokeImage must be pinned by sha256 digest" -}}
  {{- end -}}
  {{- $expectedTotal := mul (int .Values.readiness.expectedGpuNodes) (int .Values.readiness.expectedGpuCountPerNode) -}}
  {{- if gt (int .Values.readiness.fullNodeGpuCount) $expectedTotal -}}
    {{- fail "readiness.fullNodeGpuCount exceeds expected total GPU capacity" -}}
  {{- end -}}
  {{- if and .Values.compatibility.allowPreinstalledDriverFallback $gpuOperator.driver.enabled -}}
    {{- fail "preinstalled-driver fallback requires gpu-operator.driver.enabled=false" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
