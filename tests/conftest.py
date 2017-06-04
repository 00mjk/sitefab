import os
import pytest
from SiteFab.SiteFab import SiteFab
from SiteFab import utils

TEST_ROOT_DIR = os.path.dirname(__file__)

# make base object available test wides

@pytest.fixture(scope="session")
def sitefab():
    "load a valid instance of sitefab"
    fname = os.path.join(TEST_ROOT_DIR, "data/config/valid_config.yaml")
    return SiteFab(fname)

@pytest.fixture(scope="session")
def empty_post():
    "mock a post"
    post = utils.create_objdict()
    post.md = "" 
    post.html = ""
    post.meta = utils.create_objdict()
    post.statistics = utils.create_objdict()
    post.toc = utils.create_objdict()
    post.elements = utils.create_objdict()
    return post
