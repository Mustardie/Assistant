#!/usr/bin/env bash
# Copy the panel into the CEP folder and re-evaluate the host script in the
# running Premiere. Seconds instead of a full application restart.
set -e
python -m premiere.install --force >/dev/null
python -m scripts.premiere_host reload
