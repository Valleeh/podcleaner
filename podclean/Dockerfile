# The image the server runs in.
#
# Two stages, because the toolchain is 400 MB and the thing it makes is one static file.
# The runtime stage is not `scratch`: the server talks TLS to two paid endpoints, and a
# binary with no certificate store passes every test in this repository -- the suite is
# all plain HTTP to localhost -- and then fails every handshake in production.
#
# Nothing else is installed. The server needs no ffmpeg: it removes advertising by
# leaving whole MP3 frames out, in process, which is why it fits on a small host.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /podclean ./cmd/podclean

FROM alpine:3.20
RUN apk add --no-cache ca-certificates \
 && adduser -D -u 1000 -h /var/lib/podclean podclean
COPY --from=build /podclean /usr/local/bin/podclean

# Where produced episodes live. One directory per episode, each about the size of the
# original, and nothing deletes them -- so this wants to be a volume that survives the
# container, or every episode is produced and paid for again.
ENV PODCLEANER_STORE_ROOT=/var/lib/podclean
VOLUME ["/var/lib/podclean"]
EXPOSE 8080

USER podclean
ENTRYPOINT ["/usr/local/bin/podclean"]
