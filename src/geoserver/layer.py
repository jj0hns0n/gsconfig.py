from geoserver.support import ResourceInfo, xml_property, write_bool, url
from geoserver.style import Style

class _attribution(object):
    def __init__(self, title, width, height):
        self.title = title
        self.width = width
        self.height = height

def _read_attribution(node):
    title = node.find("title")
    width = node.find("logoWidth")
    height = node.find("logoHeight")

    if title is not None:
        title = title.text
    if width is not None:
        width = width.text
    if height is not None:
        height = height.text

    return _attribution(title, width, height)

def _write_attribution(builder, attr):
    builder.start("attribution", dict())
    if attr.title is not None:
        builder.start("title", dict())
        builder.data(attr.title)
        builder.end("title")
    if attr.width is not None:
        builder.start("logoWidth", dict())
        builder.data(attr.width)
        builder.end("logoWidth")
    if attr.height is not None:
        builder.start("logoHeight", dict())
        builder.data(attr.height)
        builder.end("logoHeight")
    builder.end("attribution")

def _write_default_style(builder, name):
    builder.start("defaultStyle", dict())
    if name is not None:
        builder.start("name", dict())
        builder.data(name)
        builder.end("name")
    builder.end("defaultStyle")


def _write_alternate_styles(builder, styles):
    builder.start("styles", dict())
    for s in styles:
        builder.start("style", dict())
        builder.start("name", dict())
        builder.data(s.name)
        builder.end("name")
        builder.end("style")
    builder.end("styles")


class Layer(ResourceInfo):
    def __init__(self, catalog, name):
        super(Layer, self).__init__()
        self.catalog = catalog
        self.name = name

    resource_type = "layer"
    save_method = "PUT"

    @property
    def href(self):
        return url(self.catalog.service_url, ["layers", self.name + ".xml"])

    @property
    def resource(self):
        if self.dom is None: 
            self.fetch()
        name = self.dom.find("resource/name").text
        return self.catalog.get_resource(name)

    def _get_default_style(self):
        if 'default_style' in self.dirty:
            return self.dirty['default_style']
        if self.dom is None:
            self.fetch()
        name = self.dom.find("defaultStyle/name")
        # aborted data uploads can result in no default style
        if name is not None:
            style = self.catalog.get_style(name.text)
            # the default catalog.get_style may not return a valid style if it is stored in a workspace
            # in this case obtain the style be reading the style url directly:
            if style is not None:
                return style
            else:
                #atom_link = self.dom.find("defaultStyle/{atom}link[@rel]")
                #atom_link = self.dom.find("defaultStyle/link[@rel]")
                style_workspace_url = self.dom.find("defaultStyle").getchildren()[1].attrib.get("href")
                style = self.catalog.get_style_by_url(style_workspace_url)
                return style
        else:
            return None

    def _set_default_style(self, style):
        if isinstance(style, Style):
            style = style.name
        self.dirty["default_style"] = style

    def _get_alternate_styles(self):
        if "alternate_styles" in self.dirty:
            return self.dirty["alternate_styles"]
        if self.dom is None:
            self.fetch()
        styles_list = self.dom.findall("styles/style")
        #styles = self.dom.findall("styles/style/name")
        
        alternate_styles = []
        for s in styles_list:
            style = self.catalog.get_style(s.find("name").text)
            if style is not None:
                alternate_styles.append(style)
            else:
                style_workspace_url = s.getchildren()[1].attrib.get("href")
                style = self.catalog.get_style_by_url(style_workspace_url)
                alternate_styles.append(style)
        return alternate_styles

    def _set_alternate_styles(self, styles):
        self.dirty["alternate_styles"] = styles

    default_style = property(_get_default_style, _set_default_style)
    styles = property(_get_alternate_styles, _set_alternate_styles)

    attribution_object = xml_property("attribution", _read_attribution)
    enabled = xml_property("enabled", lambda x: x.text == "true")
    advertised = xml_property("advertised", lambda x: x.text == "true", default=True)
    
    def _get_attr_text(self):
        return self.attribution_object.title

    def _set_attr_text(self, text):
        self.dirty["attribution"] = _attribution(
                    text,
                    self.attribution_object.width,
                    self.attribution_object.height
                    )
        assert self.attribution_object.title == text

    attribution = property(_get_attr_text, _set_attr_text)

    writers = dict(
            attribution = _write_attribution,
            enabled = write_bool("enabled"),
            advertised = write_bool("advertised"),
            default_style = _write_default_style,
            alternate_styles = _write_alternate_styles
            )
