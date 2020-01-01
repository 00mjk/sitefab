from time import time
from setuptools import setup
from setuptools import find_packages
from setuptools.command.develop import develop
from setuptools.command.install import install
from subprocess import check_call

long_description = open("README.md").read()
version = '1.0.%s' % int(time())


def install_spacy_model():
    "we need to download the spacy model and rebuild"
    check_call("python -m spacy download en_core_web_sm".split())
    check_call("pip install -U --force  spacy-lookups-data".split())


class PostDevelopCommand(develop):
    """Post-installation for development mode."""
    def run(self):
        install_spacy_model()
        develop.run(self)


class PostInstallCommand(install):
    """Post-installation for installation mode."""
    def run(self):
        install_spacy_model()
        install.run(self)


setup(
    name='sitefab',
    version=version,
    description='A flexible yet simple website static generator for humans',
    long_description=long_description,
    long_description_content_type="text/markdown",
    author='Elie Bursztein',
    author_email='code@elie.net',
    url='https://github.com/ebursztein/sitefab',
    license='Apache 2',
    install_requires=[
            'pyyaml',
            'jinja2',
            'tqdm',
            'termcolor',
            'yapsy',
            'gensim',
            '3to2',
            'toposort',
            'Pygments',
            'pytz',
            'diskcache',
            'xxhash',
            'pillow',
            'perfcounters',
            'terminaltables',
            'textacy',
            'spacy',
            'spacy-lookups-data',
            'mistune==0.8.4'],
    cmdclass={
            'develop': PostDevelopCommand,
            'install': PostInstallCommand
            },
    classifiers=[
            'Development Status :: 5 - Production/Stable',
            'Environment :: Console',
            'Intended Audience :: Developers',
            'Intended Audience :: Education',
            'Intended Audience :: Science/Research',
            'Intended Audience :: System Administrators',
            'License :: OSI Approved :: Apache Software License',
            'Programming Language :: Python :: 3',
            'Operating System :: MacOS',
            'Operating System :: Microsoft :: Windows',
            'Operating System :: POSIX :: Linux',
            'Topic :: Documentation',
            'Topic :: Internet :: WWW/HTTP',
            'Topic :: Internet :: WWW/HTTP :: Site Management'],
    packages=find_packages())
