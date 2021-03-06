import inspect
import os
import sys

from zope.interface import (
    implementer,
    Interface,
    )

from jinja2 import Environment, FileSystemBytecodeCache
from jinja2.exceptions import TemplateNotFound
from jinja2.loaders import FileSystemLoader
from jinja2.utils import (
    import_string,
    open_if_exists,
    )

from pyramid_jinja2.compat import reraise
from pyramid_jinja2.compat import string_types
from pyramid_jinja2.compat import text_type

from pyramid.asset import abspath_from_asset_spec
from pyramid.interfaces import ITemplateRenderer
from pyramid.resource import abspath_from_resource_spec
from pyramid.settings import asbool
from pyramid import i18n
from pyramid.threadlocal import get_current_request


class IJinja2Environment(Interface):
    pass


def maybe_import_string(val):
    if isinstance(val, string_types):
        return import_string(val.strip())
    return val


def splitlines(s):
    return filter(None, [x.strip() for x in s.splitlines()])


def parse_filters(filters):
    # input must be a string or dict
    result = {}
    if isinstance(filters, string_types):
        for f in splitlines(filters):
            name, impl = f.split('=', 1)
            result[name.strip()] = maybe_import_string(impl)
    else:
        for name, impl in filters.items():
            result[name] = maybe_import_string(impl)
    return result


def parse_multiline(extensions):
    if isinstance(extensions, string_types):
        extensions = splitlines(extensions)
    return list(extensions) # py3


class FileInfo(object):
    open_if_exists = staticmethod(open_if_exists)
    getmtime = staticmethod(os.path.getmtime)

    def __init__(self, filename, encoding='utf-8'):
        self.filename = filename
        self.encoding = encoding

    def _delay_init(self):
        if '_mtime' in self.__dict__:
            return

        f = self.open_if_exists(self.filename)
        if f is None:
            raise TemplateNotFound(self.filename)
        self._mtime = self.getmtime(self.filename)

        data = ''
        try:
            data = f.read()
        finally:
            f.close()

        if not isinstance(data, text_type):
            data = data.decode(self.encoding)
        self._contents = data

    @property
    def contents(self):
        self._delay_init()
        return self._contents

    @property
    def mtime(self):
        self._delay_init()
        return self._mtime

    def uptodate(self):
        try:
            return os.path.getmtime(self.filename) == self.mtime
        except OSError:
            return False


class _PackageFinder(object):
    inspect = staticmethod(inspect)

    def caller_package(self, allowed=()):
        f = None
        for t in self.inspect.stack():
            f = t[0]
            if f.f_globals.get('__name__') not in allowed:
                break

        if f is None:
            return None

        pname = f.f_globals.get('__name__') or '__main__'
        m = sys.modules[pname]
        f = getattr(m, '__file__', '')
        if (('__init__.py' in f) or ('__init__$py' in f)):  # empty at >>>
            return m

        pname = m.__name__.rsplit('.', 1)[0]

        return sys.modules[pname]

_caller_package = _PackageFinder().caller_package


class SmartAssetSpecLoader(FileSystemLoader):
    '''A Jinja2 template loader that knows how to handle
    asset specifications.
    '''

    def __init__(self, searchpath=(), encoding='utf-8', debug=False):
        FileSystemLoader.__init__(self, searchpath, encoding)
        self.debug = debug

    def list_templates(self):
        raise TypeError('this loader cannot iterate over all templates')

    def _get_asset_source_fileinfo(self, environment, template):
        if getattr(environment, '_default_package', None) is not None:
            pname = environment._default_package
            filename = abspath_from_asset_spec(template, pname)
        else:
            filename = abspath_from_asset_spec(template)
        fileinfo = FileInfo(filename, self.encoding)
        return fileinfo

    def get_source(self, environment, template):
        # keep legacy asset: prefix checking that bypasses
        # source path checking altogether
        if template.startswith('asset:'):
            newtemplate = template.split(':', 1)[1]
            fi = self._get_asset_source_fileinfo(environment, newtemplate)
            return fi.contents, fi.filename, fi.uptodate

        fi = self._get_asset_source_fileinfo(environment, template)
        if os.path.isfile(fi.filename):
            return fi.contents, fi.filename, fi.uptodate

        try:
            return FileSystemLoader.get_source(self, environment, template)
        except TemplateNotFound:
            ex = sys.exc_info()[1] # py2.5-3.2 compat
            message = ex.message
            message += ('; asset=%s; searchpath=%r'
                        % (fi.filename, self.searchpath))
            raise TemplateNotFound(name=ex.name, message=message)


def _get_or_build_default_environment(registry):
    environment = registry.queryUtility(IJinja2Environment)
    if environment is not None:
        return environment

    settings = registry.settings
    kw = {}
    package = _caller_package(('pyramid_jinja2', 'jinja2', 'pyramid.config'))
    reload_templates = asbool(settings.get('reload_templates', False))
    autoescape = asbool(settings.get('jinja2.autoescape', True))
    domain = settings.get('jinja2.i18n.domain', 'messages')
    debug = asbool(settings.get('debug_templates', False))
    input_encoding = settings.get('jinja2.input_encoding', 'utf-8')

    extensions = parse_multiline(settings.get('jinja2.extensions', ''))
    if 'jinja2.ext.i18n' not in extensions:
        extensions.append('jinja2.ext.i18n')

    directories = parse_multiline(settings.get('jinja2.directories') or '')
    directories = [abspath_from_resource_spec(d, package) for d in directories]
    loader = SmartAssetSpecLoader(
        directories,
        encoding=input_encoding,
        debug=debug)

    # bytecode caching
    bytecode_caching = asbool(settings.get('jinja2.bytecode_caching', True))
    bytecode_caching_directory = settings.get('jinja2.bytecode_caching_directory', None)
    if bytecode_caching:
        kw['bytecode_cache'] = FileSystemBytecodeCache(bytecode_caching_directory)

    environment = Environment(loader=loader,
                              auto_reload=reload_templates,
                              autoescape=autoescape,
                              extensions=extensions,
                              **kw)

    # register pyramid i18n functions
    wrapper = GetTextWrapper(domain=domain)
    environment.install_gettext_callables(wrapper.gettext, wrapper.ngettext)

    # register global repository for templates
    if package is not None:
        environment._default_package = package.__name__

    filters = parse_filters(settings.get('jinja2.filters', ''))
    environment.filters.update(filters)

    registry.registerUtility(environment, IJinja2Environment)
    return registry.queryUtility(IJinja2Environment)


class GetTextWrapper(object):

    def __init__(self, domain):
        self.domain = domain

    @property
    def localizer(self):
        return i18n.get_localizer(get_current_request())

    def gettext(self, message):
        return self.localizer.translate(message,
                                        domain=self.domain)

    def ngettext(self, singular, plural, n):
        return self.localizer.pluralize(singular, plural, n,
                                        domain=self.domain)


@implementer(ITemplateRenderer)
class Jinja2TemplateRenderer(object):
    '''Renderer for a jinja2 template'''
    template = None

    def __init__(self, info, environment):
        self.info = info
        self.environment = environment

    def implementation(self):
        return self.template

    @property
    def template(self):
        # get template based on searchpaths, then try relavtive one
        info = self.info
        name = info.name
        name_with_package = None
        if ':' not in name and getattr(info, 'package', None) is not None:
            package = self.info.package
            name_with_package = '%s:%s' % (package.__name__, name)
        try:
            return self.environment.get_template(name)
        except TemplateNotFound:
            if name_with_package != None:
                return self.environment.get_template(name_with_package)
            else:
                raise

    def __call__(self, value, system):
        try:
            system.update(value)
        except (TypeError, ValueError):
            ex = sys.exc_info()[1] # py2.5 - 3.2 compat
            raise ValueError('renderer was passed non-dictionary '
                             'as value: %s' % str(ex))
        return self.template.render(system)


def renderer_factory(info):
    environment = _get_or_build_default_environment(info.registry)
    return Jinja2TemplateRenderer(info, environment)


def add_jinja2_search_path(config, searchpath):
    """
    This function is added as a method of a :term:`Configurator`, and
    should not be called directly.  Instead it should be called like so after
    ``pyramid_jinja2`` has been passed to ``config.include``:

    .. code-block:: python

       config.add_jinja2_search_path('anotherpackage:templates/')

    It will add the directory or :term:`asset spec` passed as ``searchpath``
    to the current search path of the ``jinja2.environment.Environment`` used
    by :mod:`pyramid_jinja2`.
    """
    registry = config.registry
    env = _get_or_build_default_environment(registry)
    searchpath = parse_multiline(searchpath)

    for d in searchpath:
        env.loader.searchpath.append(abspath_from_resource_spec(d, config.package_name))


def add_jinja2_extension(config, ext):
    """
    This function is added as a method of a :term:`Configurator`, and
    should not be called directly.  Instead it should be called like so after
    ``pyramid_jinja2`` has been passed to ``config.include``:

    .. code-block:: python

       config.add_jinja2_extension(myext)

    It will add the Jinja2 extension passed as ``ext`` to the current
    ``jinja2.environment.Environment`` used by :mod:`pyramid_jinja2`.
    """
    env = _get_or_build_default_environment(config.registry)
    env.add_extension(ext)

def get_jinja2_environment(config):
    """
    This function is added as a method of a :term:`Configurator`, and
    should not be called directly.  Instead it should be called like so after
    ``pyramid_jinja2`` has been passed to ``config.include``:

    .. code-block:: python

       config.get_jinja2_environment()

    It will return the current ``jinja2.environment.Environment`` used by
    :mod:`pyramid_jinja2` or ``None`` if no environment has yet been set up.
    """
    return config.registry.queryUtility(IJinja2Environment)

def includeme(config):
    """Set up standard configurator registrations.  Use via:

    .. code-block:: python

       config = Configurator()
       config.include('pyramid_jinja2')

    Once this function has been invoked, the ``.jinja2`` renderer is
    available for use in Pyramid and these new directives are available as
    methods of the configurator:

    - ``add_jinja2_search_path``: Add a search path location to the search
      path.

    - ``add_jinja2_extension``: Add a list of extensions to the Jinja2
      environment.

    - get_jinja2_environment``: Return the Jinja2 ``environment.Environment``
      used by ``pyramid_jinja2``.

    """
    config.add_renderer('.jinja2', renderer_factory)
    config.add_directive('add_jinja2_search_path', add_jinja2_search_path)
    config.add_directive('add_jinja2_extension', add_jinja2_extension)
    config.add_directive('get_jinja2_environment', get_jinja2_environment)
    _get_or_build_default_environment(config.registry)
