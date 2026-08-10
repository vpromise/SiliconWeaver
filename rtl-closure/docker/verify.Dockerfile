FROM ghcr.io/librelane/librelane@sha256:ecabd075d0ddf6a2bd1cd4a32109c7dbb861ec007f7e4e423a9a081f8d23b8e2

RUN python3 -m venv /opt/rtl-closure-verify \
    && gcc_bin="$(dirname "$(find /nix/store -path '*gcc-wrapper*/bin/gcc' -print -quit)")" \
    && test -x "$gcc_bin/gcc" \
    && test -x "$gcc_bin/g++" \
    && PATH="$gcc_bin:$PATH" /opt/rtl-closure-verify/bin/pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        cocotb==2.0.1 \
        find-libpython==0.4.0 \
    && /opt/rtl-closure-verify/bin/python -c 'import cocotb; print(cocotb.__version__)'

RUN mkdir -p /opt/rtl-closure-tools/bin \
    && bitwuzla="$(find /nix/store -path '*/bin/bitwuzla' -print -quit)" \
    && test -x "$bitwuzla" \
    && ln -s "$bitwuzla" /opt/rtl-closure-tools/bin/bitwuzla

ENV PATH="/opt/rtl-closure-tools/bin:${PATH}"

LABEL org.opencontainers.image.title="RTL-Closure trusted Cocotb verifier" \
      org.opencontainers.image.version="0.4.0" \
      org.opencontainers.image.base.digest="sha256:ecabd075d0ddf6a2bd1cd4a32109c7dbb861ec007f7e4e423a9a081f8d23b8e2"
