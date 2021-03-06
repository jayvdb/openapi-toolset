from copy import deepcopy
from collections import defaultdict, OrderedDict
import json
import re
import weakref

import jsonschema
from jsonschema.validators import RefResolver
import yaml

HTTP_METHODS = [
    'OPTIONS', 'HEAD', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE'
]

class SpecError(Exception):
	pass


class MissingDoc(SpecError):
	pass


class UnmatchDoc(SpecError):
	pass


class Unset:
	pass


def strict_schema(schema, allow_additional_properties=False):
    if isinstance(schema, dict):
        if schema.get('nullable'):
            _type = schema['type']
            if not isinstance(_type, list):
                _type = [_type]
            _type.append('null')
            schema.pop('nullable')
            schema['type'] = _type
        if schema.get('type') == 'object':
            schema['allow_additional_properties'] = \
                schema.get(
                    'allow_additional_properties',
                    allow_additional_properties)
            default_required = []
            required = schema.get('required')
            properties_dct = schema.get('properties', {})
            for property_key, property_schema in properties_dct.items():
                property_schema = strict_schema(property_schema)
                schema['properties'][property_key] = property_schema
                if 'null' not in property_schema['type']:
                    default_required.append(property_key)
            schema['required'] = required or default_required
    elif isinstance(schema, list):
        schema, origin_schema = [], schema
        for item in origin_schema:
            if isinstance(item, (dict, list)):
                item = strict_schema(item)
            schema.append(item)
    return schema


class OperationSpec:
    def __init__(self, method, resource_spec):
        self.method = method
        self.resource_spec = resource_spec
        self.parameters_dict = self._initialize_parameters_dict()

    @property
    def spec_dict(self):
        return self.resource_spec.spec_dict[self.method]

    def _initialize_parameters_dict(self):
        dct = defaultdict(dict)
        parameters = self.spec_dict.get('parameters', []) + \
            self.resource_spec.spec_dict.get('parameters', [])
        for parameter in parameters:
            parameter = deepcopy(parameter)
            location = parameter.pop('in')
            name = parameter.pop('name')
            dct[location][name] = parameter
        return dct

    def get_response_body_schema(self,
                                 status_code=200,
                                 content_type='application/json'):
        responses_schema = self.spec_dict['responses']
        if status_code in responses_schema:
            schema = responses_schema[status_code]
        else:
            schema = responses_schema[str(status_code)]
        if content_type not in schema['content']:
            raise MissingDoc
        schema = schema['content'][content_type]['schema']
        schema = self.resource_spec.openapi_spec.resolve_ref(schema)
        schema = strict_schema(schema)
        return schema

    def validate_response(self,
                          content,
                          content_type='application/json',
                          charset='utf8',
                          status_code=200):
        schema = self.get_response_body_schema()
        if content_type == 'application/json':
            content = content.decode(charset)
            json_content = json.loads(content)
            try:
                jsonschema.validate(json_content, schema)
            except jsonschema.exceptions.ValidationError as err:
                raise UnmatchDoc(err)
        else:
            raise MissingDoc


class ResourceSpec:
    def __init__(self, path, openapi_spec):
        self.path = path
        self.openapi_spec = openapi_spec
        self.operations = self._initialize_operations()
        self.url_rule = self._initialize_url_rule()

    @property
    def spec_dict(self):
        return self.openapi_spec.spec_dict['paths'][self.path]

    def _initialize_operations(self):
        operations = {}
        for method in HTTP_METHODS:
            method = method.lower()
            if method not in self.spec_dict:
                continue
            operation_spec = OperationSpec(method, self)
            operations[method] = operation_spec
        return operations

    def _initialize_url_rule(self):
        operation_spec = list(self.operations.values())[0]
        parameters_dict = operation_spec.parameters_dict['path']

        def replace_parameter_name_to_regex(match):
            name = match.group('parameter')
            _type = parameters_dict.get(name)
            if _type == 'integer':
                regex = r'(?P<{}>\d+)'.format(name)
            else:
                regex = r'(?P<{}>[^/]+)'.format(name)
            return regex

        pattern_str = re.sub(
            r'{(?P<parameter>[^{}/]+)}',
            replace_parameter_name_to_regex,
            self.path
        )
        pattern_str = pattern_str.rstrip('/') + '/?$'
        return re.compile(pattern_str)


class OpenAPISpec:
    resource_spec_cls = ResourceSpec

    @classmethod
    def from_file(cls, filename):
        with open(filename) as f:
            if filename.endswith('.json'):
                spec_dict = json.load(f)
            else:
                spec_dict = yaml.safe_load(f)
        return cls(spec_dict)

    def __init__(self, spec_dict):
        self.spec_dict = spec_dict
        self.resources = self._initialize_resources()
        self.ref_resolver = RefResolver('', self.spec_dict)

    def _initialize_resources(self):
        resources = OrderedDict()
        for path in self.spec_dict['paths']:
            resource_spec = self.resource_spec_cls(path, self)
            resources[resource_spec.url_rule] = resource_spec
        return resources

    @property
    def operations(self):
        for resource in self.resources.values():
            yield from resource.operations.values()

    def get_operation_spec(self, path, method):
        # delete ending backslash or query string
        path = re.sub(r'/?([?#].*)?$', '', path)
        for url_rule, resource in self.resources.items():
            if url_rule.match(path):
                return resource.operations.get(method.lower())

    def resolve_ref(self, schema):
        schema = deepcopy(schema)

        def _resolve_ref(schema):
            """find all ref"""
            if isinstance(schema, dict):
                if '$ref' in schema:
                    schema = self.ref_resolver.resolve(schema['$ref'])[1]
                    schema = strict_schema(schema)
                    schema = _resolve_ref(schema)
                else:
                    schema = {
                        key: _resolve_ref(value)
                        for key, value in schema.items()
                    }
            elif isinstance(schema, list):
                schema = [_resolve_ref(item) for item in schema]
            return schema

        return _resolve_ref(schema)
