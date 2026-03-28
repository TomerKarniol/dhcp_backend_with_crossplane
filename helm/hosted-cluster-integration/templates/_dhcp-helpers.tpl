{{- define "dhcp.payload" -}}
{{- $v := .Values.dhcp_values | default dict -}}

{{- $dns := $v.dns | default dict -}}
{{- $dnsServers := $dns.servers | default (list) -}}
{{- $dnsDomain := $dns.domain | default "" -}}

{{- $useFailover := and (hasKey $v "failover") $v.failover -}}
{{- $failover := dict -}}
{{- if $useFailover -}}
  {{- $failover = $v.failover -}}
{{- end -}}

scopeName: {{ required "dhcp_values.scopeName is required" $v.scopeName | quote }}
network: {{ required "dhcp_values.network is required" $v.network | quote }}
subnetMask: {{ required "dhcp_values.subnetMask is required" $v.subnetMask | quote }}
startRange: {{ required "dhcp_values.startRange is required" $v.startRange | quote }}
endRange: {{ required "dhcp_values.endRange is required" $v.endRange | quote }}
leaseDurationDays: {{ required "dhcp_values.leaseDurationDays is required" $v.leaseDurationDays | int }}
description: {{ required "dhcp_values.description is required" $v.description | quote }}
gateway: {{ required "dhcp_values.gateway is required" $v.gateway | quote }}
dnsServers: {{ required "dhcp_values.dns.servers is required" $dnsServers | toJson }}
dnsDomain: {{ required "dhcp_values.dns.domain is required" $dnsDomain | quote }}
exclusions: {{ $v.exclusions | default (list) | toJson }}
failover: {{- if $useFailover }} {{ $failover | toJson }}{{- else }} null{{- end }}
{{- end }}
