{{- /*
Where the MongoDB connection string comes from.

Bundled (`mongodb.enabled`) means this chart renders it into its own
`<release>-bundled-db` Secret from the subchart's auth values; otherwise it is
`db.secretName`, a Secret the chart consumes but does not create. The two
are independent per database, so an install can bundle Redis and point at
an operated MongoDB, or the reverse.
*/ -}}
{{- define "serverInventory.mongoSecret" -}}
{{- if .Values.mongodb.enabled -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- define "serverInventory.redisSecret" -}}
{{- if .Values.redis.enabled -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- /*
The three environment variables every pod that talks to MongoDB needs —
the API and all six collector CronJobs — so a change lands in one place
instead of seven.

INVENTORY_CURSOR_SECRET is here rather than only on the API because a
collector reads the same `Settings`, and `INVENTORY_ENVIRONMENT=production`
with no cursor secret set is a startup failure
(`app.config.settings._refuse_the_dev_cursor_secret_in_production`), not a
degraded mode.

Callers must `nindent` this to their own env-list depth.
*/ -}}
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
