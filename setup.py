import os
from setuptools import find_packages, setup

with open(os.path.join(os.path.dirname(__file__), "README.md")) as readme:
    README = readme.read()

# allow setup.py to be run from any path
os.chdir(os.path.normpath(os.path.join(os.path.abspath(__file__), os.pardir)))

setup(
    name="openimis-be-provider-contract",
    # NOTE: single quotes are required here. .github/workflows/python-publish.yml
    # rewrites the version with: sed -i "s/version='.*'/version='$GIT_TAG_NAME'/g"
    version='0.1.0',
    packages=find_packages(),
    include_package_data=True,
    license="GNU AGPL v3",
    description="The openIMIS Backend Provider Contract & Empanelment reference module.",
    long_description=README,
    long_description_content_type="text/markdown",
    url="https://openimis.org/",
    author="openIMIS Community",
    install_requires=[
        "django",
        "django-simple-history",
        "django-dirtyfields",
        "cached-property",
        "openimis-be-core",
        "openimis-be-location",
        "openimis-be-medical",
        "openimis-be-medical_pricelist",
        "openimis-be-product",
        "openimis-be-claim",
    ],
    classifiers=[
        "Environment :: Web Environment",
        "Framework :: Django",
        "Intended Audience :: Developers",
        "Intended Audience :: Healthcare Industry",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering :: Medical Science Apps.",
    ],
)
