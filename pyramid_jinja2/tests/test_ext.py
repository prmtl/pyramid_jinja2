import unittest
from pyramid_jinja2.tests.base import Base, DummyRendererInfo


class Test_renderer_factory(Base, unittest.TestCase):
    def _callFUT(self, info):
        from pyramid_jinja2 import renderer_factory
        return renderer_factory(info)

    def test_with_extension(self):
        from pyramid_jinja2 import IJinja2Environment
        self.config.registry.settings.update(
            {'jinja2.directories': self.templates_dir,
             'jinja2.extensions': """
                    pyramid_jinja2.tests.extensions.TestExtension
                    """})
        info = DummyRendererInfo({
            'name': 'helloworld.jinja2',
            'package': None,
            'registry': self.config.registry,
            })
        renderer = self._callFUT(info)
        environ = self.config.registry.getUtility(IJinja2Environment)
        self.assertEqual(environ.loader.searchpath, [self.templates_dir])
        self.assertEqual(renderer.info, info)
        self.assertEqual(renderer.environment, environ)
        import pyramid_jinja2.tests.extensions
        ext = environ.extensions[
            'pyramid_jinja2.tests.extensions.TestExtension']
        self.assertEqual(ext.__class__,
                         pyramid_jinja2.tests.extensions.TestExtension)


class TestI18n(unittest.TestCase):

    def test_it(self):
        from pyramid.config import Configurator
        from pyramid_jinja2 import _get_or_build_default_environment

        c = Configurator(settings={})
        c.include('pyramid_jinja2')
        c.add_jinja2_extension('jinja2.ext.i18n')
        u = _get_or_build_default_environment(c.registry)
        self.assertTrue(hasattr(u, 'install_gettext_translations'))