# -*- coding: utf-8 -*-
import os
import re
from setuptools import setup, find_packages
import subprocess

GDAL_VERSION = subprocess.check_output(['gdal-config', '--version']).strip().decode()
GDAL_VERSION, GDAL_REVISION_MAJOR, GDAL_REVISION_MINOR = GDAL_VERSION.split('.')
GDAL_MIN = '{0}.{1}.{2}'.format(GDAL_VERSION, GDAL_REVISION_MAJOR, GDAL_REVISION_MINOR)
GDAL_MAX = '{0}.{1}.{2}'.format(GDAL_VERSION, GDAL_REVISION_MAJOR, int(GDAL_REVISION_MINOR) + 1)

here = os.path.abspath(os.path.dirname(__file__))

requirements = (
    'flask>=0.12',
    'flask-restplus==0.10.0',
    'psycopg2>=2.6.2',
    'pyyaml',
    'pygdal >= {0}, <{1}'.format(GDAL_MIN, GDAL_MAX),
    'redis',
    'py3dtiles',
    'click',
    'lazperf==0.0.9'
)

dev_requirements = (
    'pytest',
    'pytest-flask',
    'tox',
    'flake8',
    'invoke',
    'pytest-cov',
)

doc_requirements = (
    'sphinx',
    'sphinx_rtd_theme',
)

prod_requirements = (
    'uwsgi'
)


def find_version(*file_paths):
    """
    see https://github.com/pypa/sampleproject/blob/master/setup.py
    """

    with open(os.path.join(here, *file_paths), 'r') as f:
        version_file = f.read()

    # The version line must have the form
    # __version__ = 'ver'
    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]",
                              version_file, re.M)
    if version_match:
        return version_match.group(1)
    raise RuntimeError("Unable to find version string. "
                       "Should be at the first line of __init__.py.")


setup(
    name='lopocs',
    version=find_version('lopocs', '__init__.py'),
    description="Light OpenSource PointCloud Server",
    url='https://github.com/LI3DS/lopocs',
    author='dev',
    author_email='contact@oslandia.com',
    license='LGPL2 or later',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
    ],
    packages=find_packages(),
    include_package_data=True,
    test_suite='tests',
    install_requires=requirements,
    extras_require={
        'dev': dev_requirements,
        'prod': prod_requirements,
        'doc': doc_requirements
    },
    entry_points={
        'console_scripts': ['lopocs = lopocs.cli:cli'],
    }
)
