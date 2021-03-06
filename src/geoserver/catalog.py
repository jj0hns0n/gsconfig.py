from datetime import datetime, timedelta
import logging
from geoserver.layer import Layer
from geoserver.resource import FeatureType, Coverage
from geoserver.store import coveragestore_from_index, datastore_from_index, \
    UnsavedDataStore, UnsavedCoverageStore
from geoserver.style import Style, Workspace_Style
from geoserver.support import prepare_upload_bundle, url
from geoserver.layergroup import LayerGroup, UnsavedLayerGroup
from geoserver.workspace import workspace_from_index, Workspace
from os import unlink
import httplib2
import re
from xml.etree.ElementTree import XML
from xml.parsers.expat import ExpatError

from urlparse import urlparse

logger = logging.getLogger("gsconfig.catalog")

class UploadError(Exception):
    pass

class ConflictingDataError(Exception):
    pass

class AmbiguousRequestError(Exception):
    pass

class FailedRequestError(Exception):
    pass

def _name(named):
    """Get the name out of an object.  This varies based on the type of the input:
       * the "name" of a string is itself
       * the "name" of None is itself
       * the "name" of an object with a property named name is that property -
         as long as it's a string
       * otherwise, we raise a ValueError
    """
    if isinstance(named, basestring) or named is None:
        return named
    elif hasattr(named, 'name') and isinstance(named.name, basestring):
        return named.name
    else:
        raise ValueError("Can't interpret %s as a name or a configuration object" % named)

class Catalog(object):
    """
    The GeoServer catalog represents all of the information in the GeoServer
    configuration.    This includes:
    - Stores of geospatial data
    - Resources, or individual coherent datasets within stores
    - Styles for resources
    - Layers, which combine styles with resources to create a visible map layer
    - LayerGroups, which alias one or more layers for convenience
    - Workspaces, which provide logical grouping of Stores
    - Maps, which provide a set of OWS services with a subset of the server's
        Layers
    - Namespaces, which provide unique identifiers for resources
    """

    def __init__(self, service_url, username="admin", password="geoserver", disable_ssl_certificate_validation=False):
        self.service_url = service_url
        if self.service_url.endswith("/"):
            self.service_url = self.service_url.strip("/")
        self.http = httplib2.Http(
            disable_ssl_certificate_validation=disable_ssl_certificate_validation)
        self.username = username
        self.password = password
        self.http.add_credentials(self.username, self.password)
        netloc = urlparse(service_url).netloc
        self.http.authorizations.append(
                httplib2.BasicAuthentication(
                        (username, password),
                        netloc,
                        service_url,
                        {},
                        None,
                        None,
                        self.http
                        ))
        self._cache = dict()
        self._version = None

    def about(self):
        '''return the about information as a formatted html'''
        about_url = self.service_url + "/about/version.html"
        response, content = self.http.request(about_url, "GET")
        if response.status == 200:
            return content
        raise FailedRequestError('Unable to determine version: %s' %
                                 (content or response.status))

    def gsversion(self):
        '''obtain the version or just 2.2.x if < 2.3.x
        Raises:
            FailedRequestError: If the request fails.
        '''
        if self._version: return self._version
        about_url = self.service_url + "/about/version.xml"
        response, content = self.http.request(about_url, "GET")
        version = None
        if response.status == 200:
            dom = XML(content)
            resources = dom.findall("resource")
            for resource in resources:
                if resource.attrib["name"] == "GeoServer":
                    try:
                        version = resource.find("Version").text
                        break
                    except:
                        pass

        #This will raise an exception if the catalog is not available
        #If the catalog is available but could not return version information,
        #it is an old version that does not support that
        if version is None:
            self.get_workspaces()
            # just to inform that version < 2.3.x
            version = "2.2.x"
        self._version = version
        return version

    def delete(self, config_object, purge=False, recurse=False):
        """
        send a delete request
        XXX [more here]
        """
        rest_url = config_object.href

        #params aren't supported fully in httplib2 yet, so:
        params = []

        # purge deletes the SLD from disk when a style is deleted
        if purge:
            params.append("purge=true")

        # recurse deletes the resource when a layer is deleted.
        if recurse:
            params.append("recurse=true")

        if params:
            rest_url = rest_url + "?" + "&".join(params)

        headers = {
            "Content-type": "application/xml",
            "Accept": "application/xml"
        }
        response, content = self.http.request(rest_url, "DELETE", headers=headers)
        self._cache.clear()

        if response.status == 200:
            return (response, content)
        else:
            raise FailedRequestError("Tried to make a DELETE request to %s but got a %d status code: \n%s" % (rest_url, response.status, content))

    def get_xml(self, rest_url):
        logger.debug("GET %s", rest_url)

        cached_response = self._cache.get(rest_url)

        def is_valid(cached_response):
            return cached_response is not None and datetime.now() - cached_response[0] < timedelta(seconds=5)

        def parse_or_raise(xml):
            try:
                return XML(xml)
            except (ExpatError, SyntaxError), e:
                msg = "GeoServer gave non-XML response for [GET %s]: %s"
                msg = msg % (rest_url, xml)
                raise Exception(msg, e)

        if is_valid(cached_response):
            raw_text = cached_response[1]
            return parse_or_raise(raw_text)
        else:
            response, content = self.http.request(rest_url)
            if response.status == 200:
                self._cache[rest_url] = (datetime.now(), content)
                return parse_or_raise(content)
            else:
                raise FailedRequestError("Tried to make a GET request to %s but got a %d status code: \n%s" % (rest_url, response.status, content))

    def reload(self):
        reload_url = url(self.service_url, ['reload'])
        response = self.http.request(reload_url, "POST")
        self._cache.clear()
        return response

    def save(self, obj):
        """
        saves an object to the REST service

        gets the object's REST location and the XML from the object,
        then POSTS the request.
        """
        rest_url = obj.href
        message = obj.message()

        headers = {
            "Content-type": "application/xml",
            "Accept": "application/xml"
        }
        logger.debug("%s %s", obj.save_method, obj.href)
        response = self.http.request(rest_url, obj.save_method, message, headers)
        headers, body = response
        self._cache.clear()
        if 400 <= int(headers['status']) < 600:
            raise FailedRequestError("Error code (%s) from GeoServer: %s" %
                (headers['status'], body))
        return response

    def get_store(self, name, workspace=None):

        # Make sure workspace is a workspace object and not a string.
        # If the workspace does not exist, continue as if no workspace had been defined.
        if isinstance(workspace, basestring):
            workspace = self.get_workspace(workspace)

        # Create a list with potential workspaces to look into
        # if a workspace is defined, it will contain only that workspace
        # if no workspace is defined, the list will contain all workspaces.
        workspaces = []

        if workspace is None:
            workspaces.extend(self.get_workspaces())
        else:
            workspaces.append(workspace)

        # Iterate over all workspaces to find the stores or store
        found_stores = {}
        for ws in workspaces:
            # Get all the store objects from geoserver
            raw_stores = self.get_stores(workspace=ws)
            # And put it in a dictionary where the keys are the name of the store,
            new_stores = dict(zip([s.name for s in raw_stores], raw_stores))
            # If the store is found, put it in a dict that also takes into account the
            # worspace.
            if name in new_stores:
                found_stores[ws.name + ':' + name] = new_stores[name]

        # There are 3 cases:
        #    a) No stores are found.
        #    b) Only one store is found.
        #    c) More than one is found.
        if len(found_stores) == 0:
            raise FailedRequestError("No store found named: " + name)
        elif len(found_stores) > 1:
            raise AmbiguousRequestError("Multiple stores found named '" + name + "': "+ found_stores.keys())
        else:
            return found_stores.values()[0]


    def get_stores(self, workspace=None):
        if workspace is not None:
            if isinstance(workspace, basestring):
                workspace = self.get_workspace(workspace)
            ds_list = self.get_xml(workspace.datastore_url)
            cs_list = self.get_xml(workspace.coveragestore_url)
            datastores = [datastore_from_index(self, workspace, n) for n in ds_list.findall("dataStore")]
            coveragestores = [coveragestore_from_index(self, workspace, n) for n in cs_list.findall("coverageStore")]
            return datastores + coveragestores
        else:
            stores = []
            for ws in self.get_workspaces():
                a = self.get_stores(ws)
                stores.extend(a)
            return stores

    def create_datastore(self, name, workspace=None):
        if isinstance(workspace, basestring):
            workspace = self.get_workspace(workspace)
        elif workspace is None:
            workspace = self.get_default_workspace()
        return UnsavedDataStore(self, name, workspace)

    def create_coveragestore2(self, name, workspace = None):
        """
        Hm we already named the method that creates a coverage *resource*
        create_coveragestore... time for an API break?
        """
        if isinstance(workspace, basestring):
            workspace = self.get_workspace(workspace)
        elif workspace is None:
            workspace = self.get_default_workspace()
        return UnsavedCoverageStore(self, name, workspace)

    def add_data_to_store(self, store, name, data, workspace=None, overwrite = False, charset = None):
        if isinstance(store, basestring):
            store = self.get_store(store, workspace=workspace)
        if workspace is not None:
            workspace = _name(workspace)
            assert store.workspace.name == workspace, "Specified store (%s) is not in specified workspace (%s)!" % (store, workspace)
        else:
            workspace = store.workspace.name
        store = store.name

        if isinstance(data, dict):
            bundle = prepare_upload_bundle(name, data)
        else:
            bundle = data

        params = dict()
        if overwrite:
            params["update"] = "overwrite"
        if charset is not None:
            params["charset"] = charset

        headers = { 'Content-Type': 'application/zip', 'Accept': 'application/xml' }
        upload_url = url(self.service_url, 
            ["workspaces", workspace, "datastores", store, "file.shp"], params) 

        with open(bundle, "rb") as f:
            data = f.read()
            headers, response = self.http.request(upload_url, "PUT", data, headers)
            self._cache.clear()
            if headers.status != 201:
                raise UploadError(response)

    def create_featurestore(self, name, data, workspace=None, overwrite=False, charset=None):
        if not overwrite:
            try:
                store = self.get_store(name, workspace)
                msg = "There is already a store named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
            except FailedRequestError:
                # we don't really expect that every layer name will be taken
                pass

        if workspace is None:
            workspace = self.get_default_workspace()
        workspace = _name(workspace)
        params = dict()
        if charset is not None:
            params['charset'] = charset
        ds_url = url(self.service_url,
            ["workspaces", workspace, "datastores", name, "file.shp"], params)

        # PUT /workspaces/<ws>/datastores/<ds>/file.shp
        headers = {
            "Content-type": "application/zip",
            "Accept": "application/xml"
        }
        if isinstance(data,dict):
            logger.debug('Data is NOT a zipfile')
            archive = prepare_upload_bundle(name, data)
        else:
            logger.debug('Data is a zipfile')
            archive = data
        message = open(archive)
        try:
            headers, response = self.http.request(ds_url, "PUT", message, headers)
            self._cache.clear()
            if headers.status != 201:
                raise UploadError(response)
        finally:
            message.close()
            unlink(archive)

    def create_coveragestore(self, name, data, workspace=None, overwrite=False):
        if not overwrite:
            try:
                store = self.get_store(name, workspace)
                msg = "There is already a store named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
            except FailedRequestError:
                # we don't really expect that every layer name will be taken
                pass

        if workspace is None:
            workspace = self.get_default_workspace()
        headers = {
            "Content-type": "image/tiff",
            "Accept": "application/xml"
        }

        archive = None
        ext = "geotiff"

        if isinstance(data, dict):
            archive = prepare_upload_bundle(name, data)
            message = open(archive)
            if "tfw" in data:
                headers['Content-type'] = 'application/archive'
                ext = "worldimage"
        elif isinstance(data, basestring):
            message = open(data)
        else:
            message = data

        cs_url = url(self.service_url,
            ["workspaces", workspace.name, "coveragestores", name, "file." + ext])

        try:
            headers, response = self.http.request(cs_url, "PUT", message, headers)
            self._cache.clear()
            if headers.status != 201:
                raise UploadError(response)
        finally:
            if hasattr(message, "close"):
                message.close()
            if archive is not None:
                unlink(archive)

    def get_resource(self, name, store=None, workspace=None):
        if store is not None and workspace is not None:
            if isinstance(workspace, basestring):
                workspace = self.get_workspace(workspace)
            if isinstance(store, basestring):
                store = self.get_store(store, workspace)
            if store is not None:
                return store.get_resources(name)
        
        if store is not None:
            candidates = [s for s in self.get_resources(store) if s.name == name]
            if len(candidates) == 0:
                return None
            elif len(candidates) > 1:
                raise AmbiguousRequestError
            else:
                return candidates[0]

        if workspace is not None:
            for store in self.get_stores(workspace):
                resource = self.get_resource(name, store)
                if resource is not None:
                    return resource
            return None

        for ws in self.get_workspaces():
            resource = self.get_resource(name, workspace=ws)
            if resource is not None:
                return resource
        return None

    def get_resource_by_url(self, url):
        xml = self.get_xml(url)
        name = xml.find("name").text
        resource = None
        if xml.tag == 'featureType':
            resource = FeatureType
        elif xml.tag == 'coverage':
            resource = Coverage
        else:
            raise Exception('drat')
        return resource(self, None, None, name, href=url)

    def get_resources(self, store=None, workspace=None):
        if isinstance(workspace, basestring):
            workspace = self.get_workspace(workspace)
        if isinstance(store, basestring):
            store = self.get_store(store, workspace)
        if store is not None:
            return store.get_resources()
        if workspace is not None:
            resources = []
            for store in self.get_stores(workspace):
                resources.extend(self.get_resources(store))
            return resources
        resources = []
        for ws in self.get_workspaces():
            resources.extend(self.get_resources(workspace=ws))
        return resources

    def get_layer(self, name):
        try:
            lyr = Layer(self, name)
            lyr.fetch()
            return lyr
        except FailedRequestError:
            return None

    def get_layers(self, resource=None):
        if isinstance(resource, basestring):
            resource = self.get_resource(resource)
        layers_url = url(self.service_url, ["layers.xml"])
        description = self.get_xml(layers_url)
        lyrs = [Layer(self, l.find("name").text) for l in description.findall("layer")]
        if resource is not None:
            lyrs = [l for l in lyrs if l.resource.href == resource.href]
        # TODO: Filter by style
        return lyrs

    def get_layergroup(self, name=None):
        try: 
            group_url = url(self.service_url, ["layergroups", name + ".xml"])
            group = self.get_xml(group_url)
            return LayerGroup(self, group.find("name").text)
        except FailedRequestError:
            return None

    def get_layergroups(self):
        groups = self.get_xml("%s/layergroups.xml" % self.service_url)
        return [LayerGroup(self, g.find("name").text) for g in groups.findall("layerGroup")]

    def create_layergroup(self, name, layers = (), styles = (), bounds = None):
        if any(g.name == name for g in self.get_layergroups()):
            raise ConflictingDataError("LayerGroup named %s already exists!" % name)
        else:
            return UnsavedLayerGroup(self, name, layers, styles, bounds)

    def get_style(self, name):
        try:
            style_url = url(self.service_url, ["styles", name + ".xml"])
            dom = self.get_xml(style_url)
            return Style(self, dom.find("name").text)
        except FailedRequestError:
            return None

    def get_style_by_url(self, style_workspace_url):
        try:
            dom = self.get_xml(style_workspace_url)
            rest_path = style_workspace_url[re.search(self.service_url, style_workspace_url).end():]
            rest_segments = re.split("\/", rest_path)
            for i,s in enumerate(rest_segments):
                if s == "workspaces": workspace_name = rest_segments[i + 1]
            #create an instance of Workspace_Style if a workspace is contained in the
            # REST API style path (should always be the case /workspaces/<ws>/styles/<stylename>:
            if isinstance(workspace_name, basestring):
                workspace = self.get_workspace(workspace_name)
                return Workspace_Style(self, workspace, dom.find("name").text)
            else:
                return Style(self, dom.find("name").text)
            
        except FailedRequestError:
            return None

    def get_styles(self):
        styles_url = url(self.service_url, ["styles.xml"])
        description = self.get_xml(styles_url)
        return [Style(self, s.find('name').text) for s in description.findall("style")]

    def create_style(self, name, data, overwrite = False):
        if overwrite == False and self.get_style(name) is not None:
            raise ConflictingDataError("There is already a style named %s" % name)

        headers = {
            "Content-type": "application/vnd.ogc.sld+xml",
            "Accept": "application/xml"
        }

        if overwrite:
            style_url = url(self.service_url, ["styles", name + ".sld"])
            headers, response = self.http.request(style_url, "PUT", data, headers)
        else:
            style_url = url(self.service_url, ["styles"], dict(name=name))
            headers, response = self.http.request(style_url, "POST", data, headers)

        self._cache.clear()
        if headers.status < 200 or headers.status > 299: raise UploadError(response)

    def create_workspace(self, name, uri):
        xml = ("<namespace>"
            "<prefix>{name}</prefix>"
            "<uri>{uri}</uri>"
            "</namespace>").format(name=name, uri=uri)
        headers = { "Content-Type": "application/xml" }
        workspace_url = self.service_url + "/namespaces/"

        headers, response = self.http.request(workspace_url, "POST", xml, headers)
        assert 200 <= headers.status < 300, "Tried to create workspace but got " + str(headers.status) + ": " + response
        self._cache.clear()
        return self.get_workspace(name)

    def get_workspaces(self):
        description = self.get_xml("%s/workspaces.xml" % self.service_url)
        return [workspace_from_index(self, node) for node in description.findall("workspace")]

    def get_workspace(self, name):
        candidates = [w for w in self.get_workspaces() if w.name == name]
        if len(candidates) == 0:
            return None
        elif len(candidates) > 1:
            raise AmbiguousRequestError()
        else:
            return candidates[0]

    def get_default_workspace(self):
        return Workspace(self, "default")

    def set_default_workspace(self):
        raise NotImplementedError()
