# Athena native frontend

This platform-specific companion distribution contains the native Athena
terminal frontend. Install it alongside `athena-agent` on a supported Linux
host. The recommended exact-version installation is:

```bash
pip install "athena-agent==0.1.0" "athena-agent-native==0.1.0"
```

The companion wheel is Linux/architecture-specific but Python-ABI-neutral
(`py3-none-<platform>`). Python-ABI-neutral only means it does not bind to a
CPython extension ABI; it is not OS-, libc-, CPU-, or platform-neutral. The
binary and Python package versions are kept at the same release version.
