#!/usr/bin/env bash
# Build the fixtures consumed by Scylla's CQL Wasm and UDF type tests, without
# configuring or compiling the server. Commands mirror Scylla's configure.py.
set -euo pipefail

scylla_source=$(realpath "${1:?Pass the Scylla checkout path}")
cd "$scylla_source"
mkdir -p build/wasm

for source in test/resource/wasm/c/*.c; do
    name=$(basename "$source" .c)
    clang --target=wasm32 --no-standard-libraries \
        -Wl,--export-all -Wl,--no-entry "$source" -o "build/wasm/$name.wasm"
    wasm2wat "build/wasm/$name.wasm" -o "build/wasm/$name.wat"
done

cargo +1.88.0 build --target=wasm32-wasip1 --examples --locked \
    --manifest-path=test/resource/wasm/rust/Cargo.toml --target-dir=build/wasm
for source in test/resource/wasm/rust/*.rs; do
    name=$(basename "$source" .rs)
    # build.rs is Cargo's build script, not a Wasm fixture.
    if [[ "$name" == build ]]; then
        continue
    fi
    wasm-opt --enable-bulk-memory --enable-sign-ext \
        --enable-nontrapping-float-to-int --enable-mutable-globals -Oz \
        "build/wasm/wasm32-wasip1/debug/examples/$name.wasm" \
        -o "build/wasm/$name.wasm"
    wasm-strip "build/wasm/$name.wasm"
    wasm2wat "build/wasm/$name.wasm" -o "build/wasm/$name.wat"
done
