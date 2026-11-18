#!/bin/sh
set -eu

cd "$(dirname "$0")"
mkdir -p public
cp site/index.html site/style.css public/
