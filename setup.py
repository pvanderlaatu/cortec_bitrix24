from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="cortec_bitrix24",
    version="1.0.0",
    description="Migración de Bitrix24 a Frappe CRM para CORTEC",
    author="Corporación de Tecnología CORTEC S.R.L.",
    author_email="soporte@tecnocr.net",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
    license="AGPL-3.0",
)
