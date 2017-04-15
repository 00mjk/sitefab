import os
import operator
import time
from tqdm import tqdm
from collections import defaultdict
from jinja2 import Environment, FileSystemLoader
from termcolor import colored, cprint
from multiprocessing import Pool as ThreadPool 
from itertools import repeat

import files
import copy
import json

from parser.parser import Parser, parse_post
from Logger import Logger
from Plugins import Plugins
from PostCollections import PostCollections

import utils
from utils import warning, error


class SiteFab(object):
    """ Object representation of the site being built. 
    
    SiteFab main class 
    """

    SORT_BY_CREATION_DATE_DESC  = 1
    SORT_BY_CREATION_DATE = 2
    SORT_BY_UPDATE_DATE_DESC = 3
    SORT_BY_UPDATE_DATE = 4

    OK = 1
    SKIPPED = 2
    ERROR = 3

    def __init__(self, config_filename):
        
        # Timers
        self.timings  = utils.create_objdict()
        self.timings.start = time.time()
        self.timings.init_start = time.time()
        self.current_dir = os.getcwd()

        ### configuration ###
        if os.path.isfile(config_filename): #absolute path 
            self.config  = files.load_config(config_filename)
        else:
            cfg = os.path.join(files.get_site_path(), config_filename)
            if os.path.isfile(cfg):
                self.config = files.load_config(cfg)
            else:
                raise "can't find configuration: %s" % config_filename
        
        ### parser ###
        self.config.parser.templates_path =  os.path.join(files.get_site_path(),  self.config.parser.template_dir)
        self.config.parser.injected_html_templates = {} # Used to allows plugins to dynamically inject html templates.
        self.config.parser.injected_html_templates_owner = {} # store who was responsible for the injection
        self.config.parser.plugin_data = {} # store plugin data that need to be passed to the parser. E.g resized images

        ### plugins ###
        plugins_config_filename = os.path.join(files.get_site_path(), self.config.plugins_configuration_file)
        plugins_config = files.load_config(plugins_config_filename)

        debug_log_fname = os.path.join(self.get_logs_dir(), "debug.log") # where to redirect the standard python log
        self.plugins = Plugins(self.get_plugins_dir(), debug_log_fname, plugins_config)
        self.plugins_results = defaultdict(int)

        ### template rendering engine init ###
        self.jinja2 = Environment(loader=FileSystemLoader(self.get_template_dir()))
        custom_filters = self.plugins.get_template_filters()
        print custom_filters
        for flt_name, flt_fct in custom_filters.iteritems():
            self.jinja2.filters[flt_name] = flt_fct

        ### logger ###
        cfg = utils.create_objdict()
        cfg.output_dir = self.get_logs_dir()
        cfg.template_dir = os.path.join(files.get_site_path(),  self.config.logger.template_dir) #log template not the one from the users.
        cfg.log_template = "log.html"
        cfg.log_index_template = "log_index.html"
        self.logger = Logger(cfg, self)

        # finding content and assets
        self.filenames = utils.create_objdict()
        self.filenames.posts = files.get_files_list(self.get_content_dir(), "*.md")

        ### Cleanup the output directory ###
        files.clean_dir(self.get_output_dir())
        self.timings.init_stop = time.time()
        
    ### Engine Stages ###
    def preprocessing(self):
        "Perform pre-processing tasks"
        self.timings.preprocessing_start = time.time()
        self.execute_plugins([1], "SitePreparsing", " plugin")
        self.timings.preprocessing_stop = time.time()

    def parse(self):
        "parse md content into post objects"
        self.timings.parse_start = time.time()
        filenames = self.filenames.posts
        self.posts = []

        #collections creation
        min_posts = self.config.collections.min_posts
        
        tlp = self.jinja2.get_template(self.config.collections.category_template)
        path = os.path.join(self.get_output_dir(), self.config.collections.category_output_dir)
        self.posts_by_category = PostCollections(template=tlp, path=path, min_posts=min_posts)

        tlp = self.jinja2.get_template(self.config.collections.tag_template)
        path = os.path.join(self.get_output_dir(), self.config.collections.tag_output_dir)
        self.posts_by_tag = PostCollections(template=tlp, path=path, min_posts=min_posts)

        self.posts_by_template = PostCollections()
        self.posts_by_microdata = PostCollections()

        # Display injected template so the user understand who is responsible for what
        self.list_injected_html_templates()
        
        # Parsing
        cprint("\nParsing posts", "magenta")
        # thread pool creation
        tpool = ThreadPool(processes=self.config.threads)
        parser_config = utils.objdict_to_dict(self.config.parser)
        progress_bar = tqdm(total=len(filenames), unit=' files', desc="Files", leave=True)
        # chunksize = (len(filenames) / (self.config.threads * 2)) < using a different chunksize don't seems to make a huge difference
        for res in tpool.imap_unordered(parse_post, zip(filenames, repeat(parser_config)), chunksize=1):
            res = json.loads(res)
            post = utils.dict_to_objdict(res)

            if post.meta.hidden:
                # do not add hidden post
                continue
            if post.meta.creation_date_ts > int(time.time()):
                # Do not post in the future
                s = "Post in the future - skipping %s" % (post.meta.title)
                utils.warning(s)
                continue

            self.posts.append(post)

            # insert in template list
            self.posts_by_template.add(post.meta.template, post)

            # insert in microformat list
            if post.meta.microdata_type:
                self.posts_by_microdata.add(post.meta.microdata_type, post)

            ## insert in category
            if post.meta.category:
                self.posts_by_category.add(post.meta.category, post)

            ## insert in tags
            if post.meta.tags:
                for tag in post.meta.tags:
                    self.posts_by_tag.add(tag, post)

            progress_bar.update()
        
        tpool.close()
        tpool.join()
        self.timings.parse_stop = time.time()

    def process(self):
        "Processing stage"

        self.timings.process_start = time.time()
        # Posts processing
        print "\nPosts plugins"
        self.execute_plugins(self.posts, "PostProcessor", " posts")
        
        # collection processing
        print "\nCollections plugins"
        self.execute_plugins(self.posts_by_category.get_as_list(), "CollectionProcessor", " categories")
        self.execute_plugins(self.posts_by_tag.get_as_list(), "CollectionProcessor", " tags")
        self.execute_plugins(self.posts_by_template.get_as_list(), "CollectionProcessor", " templates")
        self.execute_plugins(self.posts_by_microdata.get_as_list(), "CollectionProcessor", " microdata")
        
        # site wide processing
        print "\nSite wide plugins"
        self.execute_plugins([1], "SiteProcessor", " site")
        self.timings.process_stop = time.time()

    def render(self):
        "Rendering stage"

        self.timings.render_start = time.time()
        print "\nRendering posts"
        self.render_posts()
        
        print "\nRendering collections"
        self.posts_by_category.render()
        
        print "\nAdditional Rendering"
        self.execute_plugins([1], "SiteRendering", " pages")
        self.timings.render_stop = time.time()

    def finale(self):
        "Last stage"
        
        # Last thing to do is to output the logs index
        self.logger.write_log_index()
        
        # Output
        cprint("Thread used:%s" % self.config.threads, "cyan")
        
        total_ts = round(time.time() - self.timings.start, 2)
        init_ts = round(self.timings.init_stop - self.timings.init_start,2)
        preprocessing_ts = round(self.timings.preprocessing_stop - self.timings.preprocessing_start,2)
        parsing_ts = round(self.timings.parse_stop - self.timings.parse_start, 2)
        processing_ts = round(self.timings.process_stop - self.timings.process_start, 2)
        rendering_ts = round(self.timings.render_stop - self.timings.render_start, 2)

        cprint("\nPerformance", 'magenta')
        cprint("|-Total Generation time: %s sec" % (total_ts,), "cyan")
        cprint("|--Init time: %s sec" % (init_ts), "blue")
        cprint("|--Preprocessing time: %s sec" % (preprocessing_ts), "blue")
        cprint("|--Parsing time: %s sec" % (parsing_ts), "blue")
        cprint("|--Processing time: %s sec" % (processing_ts), "blue")
        cprint("|--Rendering time: %s sec" % (rendering_ts), "blue")

        cprint("\nContent", 'magenta')
        cprint("|-Num posts: %s" % len(self.posts), "cyan")
        cprint("|-Num categories: %s" % self.posts_by_category.get_num_collections(), "cyan")
        cprint("|-Num tags: %s" % self.posts_by_tag.get_num_collections(), "cyan")
        cprint("|-Num templates: %s" % self.posts_by_template.get_num_collections(), "cyan")


        cprint("\nPlugins", 'magenta')
        cprint("|-Num plugins: %s" % len(self.plugins.plugins_enabled), "cyan")
        if self.plugins_results[self.ERROR]:            
            cprint("|-Num Errors:%s (check the logs!)" % self.plugins_results[self.ERROR], 'red')

        if self.plugins_results[self.SKIPPED]:
            cprint("|-Num Skipped:%s " % self.plugins_results[self.SKIPPED], 'yellow' )
        
    ### Post functions ###

    def render_posts(self):
        """Render posts using jinja2 templates."""
        
        for post in tqdm(self.posts, unit=' pages', miniters=1, desc="Posts"):
            template_name = "%s.html" % post.meta.template
            template = self.jinja2.get_template(template_name)
            html = post.html.decode("utf-8", 'ignore')
            rv = template.render(content=html, meta=post.meta, categories=self.posts_by_category.get_as_dict(), tags=self.posts_by_tag.get_as_dict(),            templates=self.posts_by_template.get_as_dict(), microdata=self.posts_by_microdata.get_as_dict())
            path = "%s%s/" % (self.get_output_dir(), post.meta.permanent_url)
            path = path.replace('//', '/')
            files.write_file(path, 'index.html', rv)
   
    ### Templates functions ###

    def get_num_templates(self):
        "Return the number of templates loaded."
        return len(self.jinja2.list_templates())

    def get_template_list(self):
        "Return the list of templates loaded."
        return self.jinja2.list_templates()

    ### HTML Template plugins ###
    
    def inject_parser_html_template(self, pluging_name, html_elt, jinja_template):
        """Allows plugins to replace the template of a given HTML element with their own
        Args:
            pluging_name (str): the name of the plugin responsible for the injection. Used for debug
            html_elt (str): the HTML element the template is for 
            jinja_template (str): The jinja_template that will be used to render a given element
        
        Return:
            bool: True ok, False error

        note: Due to the way plugins works a template can be overwritten many time. It is okay
        
        """

        if not html_elt or not jinja_template or not pluging_name:
            error("%s: plugin_name or html_elt or template not specified in inject_parser_template", pluging_name)
            return False
        
        self.config.parser.injected_html_templates[html_elt] = jinja_template
        self.config.parser.injected_html_templates_owner[html_elt] = pluging_name
        return True
        
    def list_injected_html_templates(self):
        "Display the list of the injected templates and which plugin was reponsible for it"
        if len(self.config.parser.injected_html_templates_owner) == 0:
            return
        cprint("\nInjected html template", "magenta")
        lst = []
        for html_elt, plugin_name in self.config.parser.injected_html_templates_owner.iteritems():
            s = "%s: %s" % (html_elt, plugin_name)
            lst.append(s)
        utils.print_color_list(lst)

    ### Plugins ###
    def execute_plugins(self, items, plugin_class, unit):
        """ Execute a given group of plugins
        
        Args:
            items (str): list of object to apply the plugins to (either collection, posts or even None) 
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
            for k, v in values.iteritems():
                self.plugins_results[k] += v


    ### Files and directories ###
    def get_site_info(self):
        "Return site information."
        return self.site
    
    def get_config(self):
        "Return sitefab config."
        return self.config
    
    def get_output_dir(self):
        "return the absolute path of the ouput dir"
        return os.path.join(files.get_site_path(), self.config.dir.output)

    def get_content_dir(self):
        "return the absolute path of the content dir"
        return os.path.join(files.get_site_path(), self.config.dir.content)
    
    def get_template_dir(self):
        "return the absolute path of the template dir"
        return os.path.join(files.get_site_path(), self.config.dir.templates)
    
    def get_logs_dir(self):
        "return the absolute path of the template dir"
        return os.path.join(files.get_site_path(), self.config.dir.logs)
    
    def get_cache_dir(self):
        "return the absolute path of the cache dir"
        return os.path.join(files.get_site_path(), self.config.dir.cache)

    def get_plugins_dir(self):
        "return the absolute path for the plugins dir"
        return os.path.join(files.get_code_path() + "/plugins/")