# Development machine only: forwards the collector server's PostgreSQL into the Compose
# network over SSH, so the local database can subscribe to it (docs/collector.md).
FROM public.ecr.aws/docker/library/alpine:3.22.2@sha256:4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412
RUN apk add --no-cache openssh-client
COPY docker/tunnel.sh /tunnel.sh
ENTRYPOINT ["sh", "/tunnel.sh"]
