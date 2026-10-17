#!/usr/bin/env bash
# The tone spliced in where advertising was cut, once per format an episode can come in.
#
# Run by hand when the tone or the set of formats changes; the files are committed and
# embedded in the server, which encodes nothing at runtime. Each is pure MPEG audio frames:
# no ID3 tag, no Xing/Info frame, and no bit reservoir, so no frame of it reaches back into
# a frame before it -- spliced between two of the publisher's, it decodes on its own.
#
# The tone: two short beeps at 880 Hz, 0.12 s each with 0.06 s between, at -26 dBFS peak
# with 20 ms ramps so neither clicks. Clearly a marker, not part of anybody's programme --
# and not loud: about -29 LUFS while it sounds, some 12 LU under the episodes it marks
# (they measured -15.6 to -18.7 LUFS on 2026-10-17). At -18 dBFS it was under 5 LU below
# them, and the owner asked for it not to be loud.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${PODCLEAN_TEST_FFMPEG_IMAGE:-podclean-ffmpeg:ci}
TONE='0.05*sin(2*PI*880*t)*(clip(min(t/0.02\,(0.12-t)/0.02)\,0\,1)+clip(min((t-0.18)/0.02\,(0.30-t)/0.02)\,0\,1))'

for format in 44100:2 48000:2 32000:2 44100:1; do
  rate=${format%:*} channels=${format#*:}
  docker run --rm --network=none --user "$(id -u):$(id -g)" -v "$PWD:/out" --entrypoint ffmpeg "$IMAGE" \
    -hide_banner -loglevel error -nostdin -y \
    -f lavfi -i "aevalsrc=${TONE}:s=${rate}:d=0.30" -ac "$channels" -ar "$rate" \
    -c:a libmp3lame -b:a 32k -reservoir 0 -write_xing 0 -id3v2_version 0 \
    "/out/${rate}-${channels}.mp3"
done
ls -l ./*.mp3
