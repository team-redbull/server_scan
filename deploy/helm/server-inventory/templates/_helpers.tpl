{{- /*
Where each connection string comes from.

The chart composes one into its own `<release>-bundled-db` Secret only when
it has the password to compose it *with* — bundled, and with the auth values
in this chart's own values. Everything else reads `db.secretName`, a Secret
the chart consumes but does not create: an externally provisioned database,
and equally a bundled one whose password comes from `auth.existingSecret`
(Vault, External Secrets, `oc create secret`), where the chart never sees the
password and so cannot build a URI around it — that Secret carries the URI
alongside the password.

The two databases are resolved independently, so an install can bundle Redis
and point at an operated MongoDB, or the reverse.
*/ -}}
{{- /* Emit "true" or NOTHING — a define always returns a string, and a
       bare `and` returns the literal "false", which every `if` in Helm
       reads as truthy. */ -}}
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

{{- /*
The two images, tag included.

`image.tag` is empty by default and falls back to the chart's `appVersion`,
so an install is pinned to the release the chart was published for rather
than to a floating tag. `latest` was the previous default and was actively
harmful here: with `imagePullPolicy: IfNotPresent` a node that already holds
a `latest` layer never re-pulls it, so a cluster silently keeps running
whatever it first pulled — even across a pod delete, and even after a new
release. Pinning makes an upgrade a value change, which is a thing GitOps
can see and roll back.

Override `tag` to run a specific build (a `sha-<commit>` tag, or an older
release) without touching the chart.
*/ -}}
{{- define "serverInventory.apiImage" -}}
{{ .Values.backend.image.repository }}:{{ .Values.backend.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "serverInventory.frontendImage" -}}
{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag | default .Chart.AppVersion }}
{{- end -}}
