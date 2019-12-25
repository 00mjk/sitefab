import os
import sys
import time
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict
from jinja2 import Environment, FileSystemLoader
from termcolor import cprint
from perfcounters import PerfCounters

from sitefab import files
from sitefab.parser import Parser
from sitefab.Logger import Logger
from sitefab.Plugins import Plugins
from sitefab.PostCollections import PostCollections
from sitefab.linter.linter import Linter
from sitefab import utils


class SiteFab(object):
    """ Object representation of the site being built.

    SiteFab main class
    """

    SORT_BY_CREATION_DATE_DESC = 1
    SORT_BY_CREATION_DATE = 2
    SORT_BY_UPDATE_DATE_DESC = 3
    SORT_BY_UPDATE_DATE = 4

    OK = 1
    SKIPPED = 2
    ERROR = 3

    def __init__(self, config_filename, version='1.0'):

        # Timers
        self.cnts = PerfCounters()
        self.cnts.start('Overall')
        self.cnts.start('Init')

        # [configuration]
        self.current_dir = Path.cwd()
        # make the configuration path absolute to avoid weird cases
        config_filename = Path(config_filename).resolve()
        if not config_filename:
            raise Exception("Supply a configuration filename")

        # FIXME: its a mess -- refactor and test the config is logged.
        if config_filename.is_file():  # absolute path
            self.config = files.load_config(config_filename)
        else:
            cfg = files.get_site_path() / config_filename
            if os.path.isfile(cfg):
                self.config = files.load_config(cfg)
            else:
                utils.error("Configuration file not found: %s" %
                            config_filename)

        # site root dir is -1 from where the config is
        self.config.root_dir = config_filename.parents[1]
        self.config.build = utils.create_objdict()

        # expose sitefab version to the templates
        self.config.build.sitefab_version = version

        # [parser] #

        # initialize the parser config
        parser_tpl_path = Path(self.config.parser.template_dir)
        self.config.parser.templates_path = (self.config.root_dir /
                                             parser_tpl_path)

        self.config.parser = Parser.make_config(self.config.parser)

        # [plugins] #
        plugins_config_filename = (self.config.root_dir /
                                   self.config.plugins_configuration_file)
        plugins_config = files.load_config(plugins_config_filename)

        # where to redirect the standard python log
        debug_log_fname = self.get_logs_dir() / "debug.log"
        self.plugins = Plugins(self.get_plugins_dir(),
                               debug_log_fname, plugins_config)
        # Store data generated by plugins that can be used later.
        self.plugin_data = {}
        self.plugin_results = defaultdict(int)

        # [template rendering engine] #
        self.jinja2 = Environment(loader=FileSystemLoader(
                                  str(self.get_template_dir())),
                                  extensions=['jinja2.ext.do'])

        # loading templates custom functions
        custom_filters = self.plugins.get_template_filters()
        for flt_name, flt_fct in custom_filters.items():
            self.jinja2.filters[flt_name] = flt_fct

        # [logger] #
        cfg = utils.create_objdict()
        cfg.output_dir = self.get_logs_dir()
        # log template not the one from the users.
        cfg.template_dir = (self.config.root_dir /
                            self.config.logger.template_dir)

        tpl_dir = self.config.root_dir / Path(self.config.logger.template_dir)
        self.config.logger.template_dir = tpl_dir
        cfg.log_template = "log.html"
        cfg.log_index_template = "log_index.html"  # noqa
        cfg.stats_template = "stats.html"
        self.logger = Logger(cfg, self)

        # [linter] #
        linter_config_filename = (self.config.root_dir /
                                  self.config.linter.configuration_file)
        linter_config = files.load_config(linter_config_filename)

        linter_config.report_template_file = (
            self.config.root_dir / self.config.linter.report_template_file)

        linter_config.output_dir = self.get_logs_dir()
        linter_config.site_output_dir = self.get_output_dir()
        self.linter = Linter(linter_config)

        # Finding content and assets.
        self.filenames = utils.create_objdict()
        self.filenames.posts = files.get_files_list(self.get_content_dir(),
                                                    "*.md")

        # Cleanup the output directories.
        files.clean_dir(self.get_output_dir())
        self.cnts.stop('Init')

    def preprocessing(self):
        "Perform pre-processing tasks"
        self.cnts.start('Preprocessing')
        self.execute_plugins([1], "SitePreparsing", " plugin")
        self.cnts.stop('Preprocessing')

    def parse(self):
        "parse md content into post objects"
        self.cnts.start('Parsing')
        filenames = self.filenames.posts
        self.posts = []

        # collections creation
        min_posts = self.config.collections.min_posts

        # posts_by_tag is what is rendered: it contains for as given post both
        #  its tags and its category
        tlp = self.jinja2.get_template(self.config.collections.template)
        path = self.get_output_dir() / self.config.collections.output_dir

        self.posts_by_tag = PostCollections(
            site=self, template=tlp, output_path=path,
            web_path=self.config.collections.output_dir, min_posts=min_posts)

        self.posts_by_category = PostCollections(
            site=self, web_path=self.config.collections.output_dir)

        self.posts_by_template = PostCollections(site=self)

        self.posts_by_microdata = PostCollections(site=self)

        # Parsing
        cprint("\nParsing posts", "magenta")
        progress_bar = tqdm(total=len(filenames),
                            unit=' files', desc="Files", leave=True)
        errors = []
        post_idx = 1
        # NOTE: passing self.config.parser might seems strange
        # but it is required to get the pluging workings
        parser = Parser(self.config.parser, self)
        for filename in filenames:
            file_content = files.read_file(filename)
            post = parser.parse(file_content)
            post.filename = filename
            post.id = post_idx
            post_idx += 1

            # do not process hidden post
            if post.meta.hidden:
                continue

            # Ignore posts set for future date
            if post.meta.creation_date_ts > int(time.time()):
                s = "Post in the future - skipping %s" % (post.meta.title)
                utils.warning(s)
                continue

            self.posts.append(post)

            # insert in template list
            if not post.meta.template:
                errors += "%s has no template defined." % post.filename
                errors += "Can't render it"
                continue

            self.posts_by_template.add(post.meta.template, post)

            # insert in microformat list
            if post.meta.microdata_type:
                self.posts_by_microdata.add(post.meta.microdata_type, post)

            # insert in category
            if post.meta.category:
                self.posts_by_category.add(post.meta.category, post)
                # tags is what is rendered
                self.posts_by_tag.add(post.meta.category, post)

            # insert in tags
            if post.meta.tags:
                for tag in post.meta.tags:
                    self.posts_by_tag.add(tag, post)

            progress_bar.update(1)

        if len(errors):
            utils.error("\n".join(errors))

        self.cnts.stop('Parsing')

    def process(self):
        "Processing stage"

        self.cnts.start('Processing')

        # Posts processing
        print("\nPosts plugins")
        self.execute_plugins(self.posts, "PostProcessor", " posts")

        # Collection processing
        print("\nCollections plugins")
        self.execute_plugins(self.posts_by_category.get_as_list(),
                             "CollectionProcessor", " categories")
        self.execute_plugins(self.posts_by_tag.get_as_list(),
                             "CollectionProcessor", " tags")
        self.execute_plugins(self.posts_by_template.get_as_list(),
                             "CollectionProcessor", " templates")
        self.execute_plugins(self.posts_by_microdata.get_as_list(),
                             "CollectionProcessor", " microdata")

        # site wide processing
        print("\nSite wide plugins")
        self.execute_plugins([1], "SiteProcessor", " site")
        self.cnts.stop('Processing')

    def render(self):
        "Rendering stage"

        self.cnts.start('Rendering')
        print("\nRendering posts")
        self.render_posts()
        quit()
        print("\nRendering collections")
        self.posts_by_tag.render()

        print("\nAdditional Rendering")
        self.execute_plugins([1], "SiteRendering", " pages")
        self.cnts.stop('Rendering')

    def finale(self):
        "Last stage"

        # Write reminainig logs
        self.logger.write_log_index()
        self.logger.write_stats()

        # Terminal recap
        cprint("Thread used:%s" % self.config.threads, "cyan")

        cprint("\nPerformance", 'magenta')
        self.cnts.stop_all()
        self.cnts.report()

        cprint("Content", 'magenta')
        cprint("|-Num posts: %s" % len(self.posts), "cyan")
        cprint("|-Num categories: %s" %
               self.posts_by_category.get_num_collections(), "cyan")
        cprint("|-Num tags: %s" %
               self.posts_by_tag.get_num_collections(), "cyan")
        cprint("|-Num templates: %s" %
               self.posts_by_template.get_num_collections(), "cyan")

        cprint("\nPlugins", 'magenta')
        cprint("|-Num plugins: %s" % len(self.plugins.plugins_enabled), "cyan")
        if self.plugin_results[self.ERROR]:
            cprint("|-Num Errors:%s (check the logs!)" %
                   self.plugin_results[self.ERROR], 'red')

        if self.plugin_results[self.SKIPPED]:
            cprint("|-Num Skipped:%s " %
                   self.plugin_results[self.SKIPPED], 'yellow')

        cprint("\nLinter", 'magenta')
        self.linter.render_report()
        if self.linter.num_post_with_errors():
            cprint("|-Num post with errors:%s (check the logs!)" %
                   self.linter.num_post_with_errors(), 'red')

        if self.linter.num_post_with_warnings():
            cprint("|-Num post with warning:%s" %
                   self.linter.num_post_with_warnings(), 'yellow')

    # Post functions #
    def render_posts(self):
        """Render posts using jinja2 templates."""
        for post in tqdm(self.posts, unit=' pages', miniters=1, desc="Posts"):
            template_name = "%s.html" % post.meta.template
            template = self.jinja2.get_template(template_name)
            rv = template.render(content=post.html, meta=post.meta,
                                 posts=self.posts,
                                 plugin_data=self.plugin_data,
                                 config=self.config,
                                 categories=self.posts_by_category.get_as_dict(),  # noqa: E501
                                 tags=self.posts_by_tag.get_as_dict(),
                                 templates=self.posts_by_template.get_as_dict(),  # noqa: E501
                                 microdata=self.posts_by_microdata.get_as_dict()  # noqa: E501
                                )

            # Liniting
            linter_results = self.linter.lint(post, rv, self)
            # Are we stopping on linting errors?
            if linter_results.has_errors and self.config.linter.stop_on_error:
                print(post.filename)
                for err in linter_results.info:
                    print("\t-%s:%s" % (err[0], err[1]))
                sys.exit(-1)

            path = self.get_output_dir() / post.meta.permanent_url
            files.write_file(path, 'index.html', rv)

    # Templates functions #
    def get_num_templates(self):
        "Return the number of templates loaded."
        return len(self.jinja2.list_templates())

    def get_template_list(self):
        "Return the list of templates loaded."
        return self.jinja2.list_templates()

    # Plugins #
    def execute_plugins(self, items, plugin_class, unit):
        """ Execute a given group of plugins

        Args:
            items (str): list of object to apply the plugins to
                         {collection, posts, None}
            plugin_class (str):  which type of plugins to execute
            unit (str): which unit to report in the progress bar

        Return:
            None
        """
        results = self.plugins.run_plugins(items, plugin_class, unit, self)
        self.plugins.display_execution_results(results, self)

        # sum all plugins data for recap
        for result in results:
            plugin, values = result
            for k, v in values.items():
                self.plugin_results[k] += v

    # Files and directories #
    def get_site_info(self):
        "Return site information."
        return self.site

    def get_config(self):
        "Return sitefab config."
        return self.config

    def get_output_dir(self):
        "return the absolute path of the ouput dir"
        return self.config.root_dir / self.config.dir.output

    def get_content_dir(self):
        "return the absolute path of the content dir"
        return self.config.root_dir / self.config.dir.content

    def get_template_dir(self):
        "return the absolute path of the template dir"
        return self.config.root_dir / self.config.dir.templates

    def get_logs_dir(self):
        "return the absolute path of the log dir"
        return self.config.root_dir / self.config.dir.logs

    def get_cache_dir(self):
        "return the absolute path of the cache dir"
        return self.config.root_dir / self.config.dir.cache

    def get_plugins_dir(self):
        "return the absolute path for the plugins dir"
        return files.get_code_path() / "plugins/"
