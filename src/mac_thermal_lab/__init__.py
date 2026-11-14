"""Mac Thermal Lab — macOS Apple Silicon 热状态与功耗实验记录。"""

from importlib.metadata import PackageNotFoundError, version as _package_version

try:
    # 单一来源：版本号只写在 pyproject.toml，避免两处各自漂移
    __version__ = _package_version("mac-thermal-lab")
except PackageNotFoundError:  # 未安装、直接从源码树里跑
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
