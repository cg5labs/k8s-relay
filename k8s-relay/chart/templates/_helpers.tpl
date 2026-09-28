{{- define "k8s-deployer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "k8s-deployer.validateReplicaCount" -}}
{{- $replicas := int .Values.replicaCount -}}
{{- if or (lt $replicas 1) (eq (mod $replicas 2) 0) -}}
{{- fail "replicaCount must be a positive odd number to preserve lease-vote quorum" -}}
{{- end -}}
{{- end -}}

{{- define "k8s-deployer.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "k8s-deployer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "k8s-deployer.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "k8s-deployer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k8s-deployer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "k8s-deployer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "k8s-deployer.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "k8s-deployer.leaseName" -}}
{{- default (include "k8s-deployer.fullname" .) .Values.leaderElection.leaseName }}
{{- end }}

{{- define "k8s-deployer.postgresqlName" -}}
{{- printf "%s-postgresql" (include "k8s-deployer.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "k8s-deployer.postgresqlSecretName" -}}
{{- printf "%s-auth" (include "k8s-deployer.postgresqlName" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "k8s-deployer.postgresqlDataName" -}}
{{- printf "%s-data" (include "k8s-deployer.postgresqlName" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "k8s-deployer.postgresqlConfigName" -}}
{{- printf "%s-config" (include "k8s-deployer.postgresqlName" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "k8s-deployer.postgresqlSelectorLabels" -}}
app.kubernetes.io/name: {{ printf "%s-postgresql" (include "k8s-deployer.name" .) | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgresql
{{- end }}

{{- define "k8s-deployer.postgresqlPasswordSecretName" -}}
{{- if .Values.postgresql.auth.existingSecret }}
{{- .Values.postgresql.auth.existingSecret }}
{{- else }}
{{- include "k8s-deployer.postgresqlSecretName" . }}
{{- end }}
{{- end }}
