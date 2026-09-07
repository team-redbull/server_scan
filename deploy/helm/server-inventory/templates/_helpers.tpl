{{- /*
The environment every pod that talks to MongoDB needs — the API and all
five collector CronJobs — so a change lands in one place instead of six.

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
      name: {{ .Values.db.secretName }}
      key: mongo-uri
- name: INVENTORY_REDIS_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.db.secretName }}
      key: redis-uri
{{- if or .Values.backend.cursorSecret .Values.backend.existingCursorSecret }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.backend.existingCursorSecret | default (printf "%s-cursor-secret" .Release.Name) }}
      key: cursor-secret
{{- end }}
{{- end -}}
