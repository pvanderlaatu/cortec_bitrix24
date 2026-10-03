from setuptools import setup, find_packages

# Las dependencias de una app Frappe NO incluyen `frappe` ni las demás
# apps del bench: bench las instala por su cuenta con `bench get-app`, y
# no existen en PyPI con ese nombre. Declararlas aquí hace que
# `uv pip install -e` intente bajarlas de PyPI y reviente el build de la
# imagen. Las apps requeridas se declaran en hooks.py (required_apps).
#
# Este fichero está vacío a propósito; se filtran las líneas en blanco
# para que `install_requires` no acabe siendo [""], que setuptools
# rechaza.
with open("requirements.txt") as f:
    install_requires = [
        line.strip()
        for line in f.read().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

setup(
    name="cortec_bitrix24",
    version="1.1.5",
    description="Migración de Bitrix24 a Frappe CRM para CORTEC",
    author="Corporación de Tecnología CORTEC S.R.L.",
    author_email="soporte@tecnocr.net",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
    license="AGPL-3.0",
)
