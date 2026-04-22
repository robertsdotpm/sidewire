from setuptools import setup, find_packages

setup(
    name="sidewire",
    version="0.1.1",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=["aionetiface", "ecdsa"],
    python_requires=">=3.5",
)
