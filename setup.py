"""Package repository resources without maintaining a second copy of them."""
from setuptools import find_namespace_packages, setup

setup(
    packages=find_namespace_packages("src") + ["bioma_resources"],
    package_dir={"": "src", "bioma_resources": "."},
    exclude_package_data={"": ["**/__pycache__/*", "**/*.pyc", "**/.DS_Store"]},
)
