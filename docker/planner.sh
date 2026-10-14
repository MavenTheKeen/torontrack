#!/bin/bash
# Trip planner (docs/planner.md): build the OpenTripPlanner graph whenever the downloaded
# inputs change (manifest.json is newer than graph.obj), then serve it until they change
# again. The server is stopped while building, so peak memory is the build alone.
set -u
cd /var/opentripplanner
otp() { java $1 -cp @/app/jib-classpath-file @/app/jib-main-class-file /var/opentripplanner/ "${@:2}"; }
while true; do
  until [ -f manifest.json ]; do echo "Waiting for planner-data"; sleep 60; done
  if [ ! -f graph.obj ] || [ manifest.json -nt graph.obj ]; then
    echo "Building graph"
    if ! otp "$BUILD_JAVA_OPTS" --build --save; then
      [ -f graph.obj ] || { echo "Build failed and no graph to serve"; sleep 1800; continue; }
      echo "Build failed; serving the previous graph until the inputs change"
      touch graph.obj
    fi
  fi
  otp "$JAVA_OPTS" --load --serve &
  server=$!
  trap 'kill $server; wait $server; exit 0' TERM INT
  while kill -0 $server 2>/dev/null && [ ! manifest.json -nt graph.obj ]; do sleep 600 & wait $!; done
  kill $server 2>/dev/null
  wait $server
done
