#!/bin/bash -ex

TMP=`mktemp -d`
git archive HEAD | tar -x -C "$TMP"
sudo cp -a "$TMP"/server/. /opt/telemetry/server
rm -rf "$TMP"
