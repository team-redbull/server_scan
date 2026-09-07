{{- /* Emit "true" or nothing: a bare `and` returns the string "false", which every Helm `if` reads as truthy. */ -}}
{{- define "serverInventory.composeMongoUri" -}}
{{- if and .Values.mongodb.enabled (not .Values.mongodb.auth.existingSecret) -}}true{{- end -}}
{{- end -}}

{{- define "serverInventory.composeRedisUri" -}}
{{- if and .Values.redis.enabled (not .Values.redis.auth.existingSecret) -}}true{{- end -}}
{{- end -}}

{{- define "serverInventory.mongoSecret" -}}
{{- if include "serverInventory.composeMongoUri" . -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- define "serverInventory.redisSecret" -}}
{{- if include "serverInventory.composeRedisUri" . -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- /* The API and every collector CronJob. Collectors need the cursor secret too, or Settings refuses to start. */ -}}
{{- define "serverInventory.dbEnv" -}}
- name: INVENTORY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ include "serverInventory.mongoSecret" . }}
      key: mongo-uri
- name: INVENTORY_REDIS_URI
  valueFrom:
    secretKeyRef:
      name: {{ include "serverInventory.redisSecret" . }}
      key: redis-uri
{{- if or .Values.backend.cursorSecret .Values.backend.existingCursorSecret }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.backend.existingCursorSecret | default (printf "%s-cursor-secret" .Release.Name) }}
      key: cursor-secret
{{- end }}
{{- end -}}

{{- /* Blank tag means the chart's appVersion. Never `latest` — see deploy/README.md. */ -}}
{{- define "serverInventory.apiImage" -}}
{{ .Values.backend.image.repository }}:{{ .Values.backend.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "serverInventory.frontendImage" -}}
{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag | default .Chart.AppVersion }}
{{- end -}}
