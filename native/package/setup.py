from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel


class PlatformBinaryDistribution(Distribution):
    """Describe a package carrying a native file but no Python extension."""


class PlatformWheel(bdist_wheel):
    """Emit a platform-specific wheel without a CPython extension ABI."""

    def finalize_options(self):
        super().finalize_options()
        # Package data contains an ELF executable, so this is not a pure
        # ``py3-none-any`` wheel.  It has no CPython extension ABI, though.
        self.root_is_pure = False

    def get_tag(self):
        _python, _abi, platform = super().get_tag()
        return "py3", "none", platform


setup(
    distclass=PlatformBinaryDistribution,
    cmdclass={"bdist_wheel": PlatformWheel},
)
