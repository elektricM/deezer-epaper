#!/bin/sh
# Build the MediaRemote adapter. Plain clang, no Xcode project, no signing.
set -e
cd "$(dirname "$0")"
clang -fobjc-arc -O2 -dynamiclib -framework Foundation \
      -o mradapter.dylib mradapter.m
echo "built $(pwd)/mradapter.dylib"
