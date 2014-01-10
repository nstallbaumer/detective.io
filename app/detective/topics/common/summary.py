# -*- coding: utf-8 -*-
from app.detective.models     import Topic
from app.detective.neomatch   import Neomatch
from app.detective.register   import topics_rules
from app.detective.utils      import get_model_fields, get_topic_models, uploaded_to_tempfile, open_csv, to_underscores, to_class_name
from difflib                  import SequenceMatcher
from django.core.paginator    import Paginator, InvalidPage
from django.core.urlresolvers import resolve
from django.http              import Http404, HttpResponse
from neo4django.db            import connection
from tastypie                 import http
from tastypie.exceptions      import ImmediateHttpResponse
from tastypie.resources       import Resource
from tastypie.serializers     import Serializer
import json
import re
import logging

from .errors import *

# Get an instance of a logger
logger = logging.getLogger(__name__)

class SummaryResource(Resource):
    # Local serializer
    serializer = Serializer(formats=["json"]).serialize

    class Meta:
        allowed_methods = ['get', 'post']
        resource_name   = 'summary'
        object_class    = object

    def obj_get_list(self, request=None, **kwargs):
        # Nothing yet here!
        raise Http404("Sorry, not implemented yet!")

    def obj_get(self, request=None, **kwargs):
        content = {}
        # Get the current topic
        self.topic = self.get_topic_or_404(request=request)
        # Check for an optional method to do further dehydration.
        method = getattr(self, "summary_%s" % kwargs["pk"], None)
        if method:
            try:
                self.throttle_check(kwargs["bundle"].request)
                content = method(kwargs["bundle"], kwargs["bundle"].request)
                # Serialize content in json
                # @TODO implement a better format support
                content  = self.serializer(content, "application/json")
                # Create an HTTP response
                response = HttpResponse(content=content, content_type="application/json")
            except ForbiddenError as e:
                response = http.HttpForbidden(e)
            except UnauthorizedError as e:
                response = http.HttpUnauthorized(e)
        else:
            # Stop here, unkown summary type
            raise Http404("Sorry, not implemented yet!")
        # We force tastypie to render the response directly
        raise ImmediateHttpResponse(response=response)

    # TODO : factorize obj_get and post_detail methods
    def post_detail(self, request=None, **kwargs):
        content = {}
        # Get the current topic
        self.topic = self.get_topic_or_404(request=request)
        # Check for an optional method to do further dehydration.
        method = getattr(self, "summary_%s" % kwargs["pk"], None)
        if method:
            try:
                self.throttle_check(request)
                content = method(request, **kwargs)
                # Serialize content in json
                # @TODO implement a better format support
                content  = self.serializer(content, "application/json")
                # Create an HTTP response
                response = HttpResponse(content=content, content_type="application/json")
            except ForbiddenError as e:
                response = http.HttpForbidden(e)
            except UnauthorizedError as e:
                response = http.HttpUnauthorized(e)
        else:
            # Stop here, unkown summary type
            raise Http404("Sorry, not implemented yet!")
        raise ImmediateHttpResponse(response=response)

    def get_topic_or_404(self, request=None):
        try:
            if request is not None:
                return Topic.objects.get(module=resolve(request.path).namespace)
            else:
                return Topic.objects.get(module=self._meta.urlconf_namespace)
        except Topic.DoesNotExist:
            raise Http404()

    def summary_countries(self, bundle, request):
        app_label = self.topic.app_label()
        # Query to aggreagte relationships count by country
        query = """
            START n=node(*)
            MATCH (m)-[:`<<INSTANCE>>`]->(i)<-[*0..1]->(country)<-[r:`<<INSTANCE>>`]-(n)
            WHERE HAS(country.isoa3)
            AND HAS(n.model_name)
            AND n.model_name = 'Country'
            AND n.app_label = '%s'
            AND HAS(country.isoa3)
            RETURN country.isoa3 as isoa3, ID(country) as id, count(i)-1 as count
        """ % app_label
        # Get the data and convert it to dictionnary
        countries = connection.cypher(query).to_dicts()
        obj       = {}
        for country in countries:
            # Use isoa3 as identifier
            obj[ country["isoa3"] ] = country
            # ISOA3 is now useless
            del country["isoa3"]
        return obj

    def summary_types(self, bundle, request):
        app_label = self.topic.app_label()
        # Query to aggreagte relationships count by country
        query = """
            START n=node(*)
            MATCH (c)<-[r:`<<INSTANCE>>`]-(n)
            WHERE HAS(n.model_name)
            AND n.app_label = '%s'
            RETURN ID(n) as id, n.model_name as name, count(c) as count
        """ % app_label
        # Get the data and convert it to dictionnary
        types = connection.cypher(query).to_dicts()
        obj   = {}
        for t in types:
            # Use name as identifier
            obj[ t["name"].lower() ] = t
            # name is now useless
            del t["name"]
        return obj

    def summary_forms(self, bundle, request):
        available_resources = {}
        # Get the model's rules manager
        rulesManager = topics_rules()
        # Fetch every registered model
        # to print out its rules
        for model in get_topic_models(self.topic.module):
            name                = model.__name__.lower()
            rules               = rulesManager.model(model).all()
            fields              = get_model_fields(model)
            verbose_name        = getattr(model._meta, "verbose_name", name).title()
            verbose_name_plural = getattr(model._meta, "verbose_name_plural", verbose_name + "s").title()

            for key in rules:
                # Filter rules to keep only Neomatch
                if isinstance(rules[key], Neomatch):
                    fields.append({
                        "name"         : key,
                        "type"         : "ExtendedRelationship",
                        "verbose_name" : rules[key].title,
                        "rules"        : {},
                        "related_model": rules[key].target_model.__name__
                    })

            available_resources[name] = {
                'description'         : getattr(model, "_description", None),
                'topic'               : getattr(model, "_topic", self.topic.slug) or self.topic.slug,
                'model'               : getattr(model, "__name_", ""),
                'verbose_name'        : verbose_name,
                'verbose_name_plural' : verbose_name_plural,
                'name'                : name,
                'fields'              : fields,
                'rules'               : rules
            }

        return available_resources

    def summary_mine(self, bundle, request):
        app_label = self.topic.app_label()
        self.method_check(request, allowed=['get'])
        if not request.user.id:
            raise UnauthorizedError('This method require authentication')

        query = """
            START root=node(*)
            MATCH (type)-[`<<INSTANCE>>`]->(root)
            WHERE HAS(root.name)
            AND HAS(root._author)
            AND HAS(type.model_name)
            AND %s IN root._author
            AND type.app_label = '%s'
            RETURN DISTINCT ID(root) as id, root.name as name, type.name as model
        """ % ( int(request.user.id), app_label )

        matches      = connection.cypher(query).to_dicts()
        count        = len(matches)
        limit        = int(request.GET.get('limit', 20))
        paginator    = Paginator(matches, limit)

        try:
            p     = int(request.GET.get('page', 1))
            page  = paginator.page(p)
        except InvalidPage:
            raise Http404("Sorry, no results on that page.")

        objects = []
        for result in page.object_list:
            label = result.get("name", None)
            objects.append({
                'label': label,
                'subject': {
                    "name": result.get("id", None),
                    "label": label
                },
                'predicate': {
                    "label": "is instance of",
                    "name": "<<INSTANCE>>"
                },
                'object': result.get("model", None)
            })

        object_list = {
            'objects': objects,
            'meta': {
                'page': p,
                'limit': limit,
                'total_count': count
            }
        }

        return object_list


    def summary_search(self, bundle, request):
        self.method_check(request, allowed=['get'])

        if not "q" in request.GET: raise Exception("Missing 'q' parameter")

        limit     = int(request.GET.get('limit', 20))
        query     = bundle.request.GET["q"].lower()
        results   = self.search(query)
        count     = len(results)
        paginator = Paginator(results, limit)

        try:
            p     = int(request.GET.get('page', 1))
            page  = paginator.page(p)
        except InvalidPage:
            raise Http404("Sorry, no results on that page.")

        objects = []
        for result in page.object_list:
            objects.append(result)

        object_list = {
            'objects': objects,
            'meta': {
                'q': query,
                'page': p,
                'limit': limit,
                'total_count': count
            }
        }

        self.log_throttled_access(request)
        return object_list

    def summary_rdf_search(self, bundle, request):
        self.method_check(request, allowed=['get'])

        limit     = int(request.GET.get('limit', 20))
        query     = json.loads(request.GET.get('q', 'null'))
        subject   = query.get("subject", None)
        predicate = query.get("predicate", None)
        obj       = query.get("object", None)
        results   = self.rdf_search(subject, predicate, obj)
        count     = len(results)
        paginator = Paginator(results, limit)
        try:
            p     = int(request.GET.get('page', 1))
            page  = paginator.page(p)
        except InvalidPage:
            raise Http404("Sorry, no results on that page.")

        objects = []
        for result in page.object_list:
            objects.append(result)

        object_list = {
            'objects': objects,
            'meta': {
                'q': query,
                'page': p,
                'limit': limit,
                'total_count': count
            }
        }

        self.log_throttled_access(request)
        return object_list

    def summary_human(self, bundle, request):
        self.method_check(request, allowed=['get'])

        if not "q" in request.GET:
            raise Exception("Missing 'q' parameter")

        query        = request.GET["q"]
        # Find the kown match for the given query
        matches      = self.find_matches(query)
        # Build and returns a list of proposal
        propositions = self.build_propositions(matches, query)
        # Build paginator
        count        = len(propositions)
        limit        = int(request.GET.get('limit', 20))
        paginator    = Paginator(propositions, limit)

        try:
            p     = int(request.GET.get('page', 1))
            page  = paginator.page(p)
        except InvalidPage:
            raise Http404("Sorry, no results on that page.")

        objects = []
        for result in page.object_list:
            objects.append(result)

        object_list = {
            'objects': objects,
            'meta': {
                'q': query,
                'page': p,
                'limit': limit,
                'total_count': count
            }
        }

        self.log_throttled_access(request)
        return object_list

    def summary_bulk_upload(self, request, **kwargs):
        # Define Exceptions
        topic = self.topic
        class Error (Exception):
            """
            Generic Custom Exception for this endpoint.
            Include the topic.
            """
            def __init__(self, **kwargs):
                """ set the topic and add all the parameters as attributes """
                self.topic = topic
                for key, value in kwargs.items():
                    setattr(self, key, value)
            def __str__(self):
                return self.__dict__

        class AttributeDoesntExist (Error): pass
        class WrongCSVSyntax       (Error): pass
        class ColumnUnknow         (Error): pass
        class ModelDoesntExist     (Error): pass
        class RelationDoesntExist  (Error): pass
        class ValidationError      (Error): pass
        # only allow POST requests
        self.method_check(request, allowed=['post'])

        # check session
        if not request.user.id:
            raise UnauthorizedError('This method require authentication')

        entities   = {}
        relations  = []
        errors     = []
        id_mapping = {}

        try:
            # retrieve all models in current topic
            all_models = dict((model.__name__, model) for model in get_topic_models(self.topic.module))

            # flattern the list of files
            files = [file for sublist in request.FILES.lists() for file in sublist[1]]
            # iterate over all files and dissociate entities .csv from relations .csv
            for file in files:
                csv_reader = open_csv(file)
                header = csv_reader.next()
                assert len(header) > 1, "header should have at least 2 columns"
                assert header[0].endswith("_id"), "First column should begin with a header like <model_name>_id"
                if len(header) >=3 and header[0].endswith("_id") and header[2].endswith("_id"):
                    # this is a relationship file
                    relations.append(file)
                else:
                    # this is an entities file
                    model_name = to_class_name(header[0].replace("_id", ""))
                    if model_name in all_models.keys():
                        entities[model_name] = file
                    else:
                        raise ModelDoesntExist(model=model_name, file=file)

            # first iterate over entities
            for entity, file in entities.items():
                tempfile   = uploaded_to_tempfile(file)
                csv_reader = open_csv(tempfile)
                header     = csv_reader.next()
                # must check that all columns map to an existing model field
                field_names = [field['name'] for field in get_model_fields(all_models[entity])]
                columns = []
                for column in header[1:]:
                    column = to_underscores(column)
                    if column is not '':
                        if not column in field_names:
                            raise ColumnUnknow(file=file.name, column=column, model=entity, attributes_available=field_names)
                            break
                        columns.append(column)
                else:
                    # here, we know that all columns are valid
                    for row in csv_reader:
                        data = {}
                        id   = row[0]
                        for i, column in enumerate(columns):
                            data[column] = str(row[i+1]).decode('utf-8')
                        # instanciate a model
                        # NOTE: The following line is not working, it could raise a ValidationError for nothing. Don't know why.
                        # Then we iterate over all the parameters and use setattr.
                        # item = all_models[entity].objects.create(**data)
                        item = all_models[entity].objects.create()
                        for key, value in data.items():
                            try:
                                setattr(item, key, value)
                            except Exception as e:
                                raise ValidationError(data=data, model=entity, key=key, value=value, error=e)
                        # map the object with the ID defined in the .csv
                        id_mapping[(entity, id)] = item
                # closing a tempfile deletes it
                tempfile.close()

            inserted_relations = 0
            # then iterate over relations
            for file in relations:
                tempfile = uploaded_to_tempfile(file)
                # create a csv reader
                csv_reader    = open_csv(tempfile)
                csv_header    = csv_reader.next()
                relation_name = to_underscores(csv_header[1])
                model_from    = to_class_name(csv_header[0].replace("_id", ""))
                model_to      = to_class_name(csv_header[2].replace("_id", ""))
                # check that the relation actually exists between the two objects
                try:
                    getattr(all_models[model_from], relation_name)
                except Exception as e:
                    raise RelationDoesntExist(
                        file             = file.name,
                        model_from       = model_from,
                        model_to         = model_to,
                        relation_name    = relation_name,
                        fields_available = [field['name'] for field in get_model_fields(all_models[model_from])],
                        error            = e)
                for row in csv_reader:
                    id_from = row[0]
                    id_to   = row[2]
                    if id_to and id_from:
                        try:
                            getattr(id_mapping[(model_from, id_from)], relation_name).add(id_mapping[(model_to, id_to)])
                            inserted_relations += 1
                        except Exception as e:
                            raise Error(
                                file             = file.name,
                                model_from       = model_from,
                                id_from          = id_from,
                                model_to         = model_to,
                                id_to            = id_to,
                                relation_name    = relation_name,
                                error            = e)
                    else:
                        # A key is missing (id_from or id_to) but we don't want to stop the parsing.
                        # Then we store the wrong line to return it to the user.
                        errors.append(dict(WarningInformationIsMissing=dict(file=file.name, row=row, line=csv_reader.line_num)))

                tempfile.close()

            # Save everything if all is ok
            saved = 0
            if not errors:
                for item in id_mapping.values():
                    item.save()

            self.log_throttled_access(request)
            return {
                'inserted' : {
                    'objects' : saved,
                    'links'   : saved > 0 and inserted_relations or saved
                },
                "errors" : errors
            }
        except Exception as e:
            import traceback
            logger.error(traceback.format_exc())
            return {
                "errors" : [{e.__class__.__name__ : e}]
            }

    def summary_syntax(self, bundle, request): return self.get_syntax(bundle, request)

    def search(self, query):
        match = str(query).lower()
        match = re.sub("\"|'|`|;|:|{|}|\|(|\|)|\|", '', match).strip()
        # Query to get every result
        query = """
            START root=node(*)
            MATCH (root)<-[r:`<<INSTANCE>>`]-(type)
            WHERE HAS(root.name)
            AND LOWER(root.name) =~ '.*(%s).*'
            AND type.app_label = '%s'
            RETURN ID(root) as id, root.name as name, type.model_name as model
        """ % (match, self.topic.app_label() )
        return connection.cypher(query).to_dicts()

    def rdf_search(self, subject, predicate, obj):
        # Query to get every result
        query = """
            START st=node(*)
            MATCH (st)<-[:`%s`]-(root)<-[:`<<INSTANCE>>`]-(type)
            WHERE HAS(root.name)
            AND HAS(st.name)
            AND type.name = "%s"
            AND st.name = "%s"
            AND type.app_label = '%s'
            RETURN DISTINCT ID(root) as id, root.name as name, type.model_name as model
        """ % ( predicate["name"], subject["name"], obj["name"], self.topic.app_label() )
        return connection.cypher(query).to_dicts()


    def get_models_output(self):
        # Select only some atribute
        output = lambda m: {'name': self.topic.slug + ":" + m.__name__, 'label': m._meta.verbose_name.title()}
        return [ output(m) for m in get_topic_models(self.topic.module) ]


    def ngrams(self, input):
        input = input.split(' ')
        output = []
        end = len(input)
        for n in range(1, end+1):
            for i in range(len(input)-n+1):
                output.append(input[i:i+n])
        return output

    def get_close_labels(self, token, lst, ratio=0.6):
        """
            Look for the given token into the list using labels
        """
        matches = []
        for item in lst:
            cpr = item["label"]
            if SequenceMatcher(None, token, cpr).ratio() >= ratio:
                matches.append(item)
        return matches

    def find_matches(self, query):
        # Group ngram by following string
        ngrams  = [' '.join(x) for x in self.ngrams(query) ]
        matches = []
        models  = self.get_syntax()["subject"]["model"]
        rels    = self.get_syntax()["predicate"]["relationship"]
        # Known models lookup for each ngram
        for token in ngrams:
            obj = {
                'models'       : self.get_close_labels(token, models),
                'relationships': self.get_close_labels(token, rels),
                'token'        : token
            }
            matches.append(obj)
        return matches

    def build_propositions(self, matches, query):
        """
            For now, a proposition follow the form
            <subject> <predicat> <object>
            Where a <subject>, is an "Named entity" or a Model
            a <predicat> is a relationship type
            and an <object> is a "Named entity" or a Model.
            Later, as follow RDF standard, an <object> could be any data.
        """
        def remove_duplicates(lst):
            seen = set()
            new_list = []
            for item in lst:
                # Create a hash of the dictionary
                obj = hash(frozenset(item.items()))
                if obj not in seen:
                    seen.add(obj)
                    new_list.append(item)
            return new_list

        def is_preposition(token=""):
            return str(token).lower() in ["aboard", "about", "above", "across", "after", "against",
            "along", "amid", "among", "anti", "around", "as", "at", "before", "behind", "below",
            "beneath", "beside", "besides", "between", "beyond", "but", "by", "concerning",
            "considering",  "despite", "down", "during", "except", "excepting", "excluding",
            "following", "for", "from", "in", "inside", "into", "like", "minus", "near", "of",
            "off", "on", "onto", "opposite", "outside", "over", "past", "per", "plus", "regarding",
            "round", "save", "since", "than", "through", "to", "toward", "towards", "under",
            "underneath", "unlike", "until", "up", "upon", "versus", "via", "with", "within", "without"]

        def previous_word(sentence="", base=""):
            if base == "" or sentence == "": return ""
            parts = sentence.split(base)
            return parts[0].strip().split(" ")[-1] if len(parts) else None

        predicates    = []
        subjects      = []
        objects       = []
        propositions  = []
        # Picks candidates for subjects and predicates
        for match in matches:
            subjects   += match["models"]
            predicates += match["relationships"]
            # Objects are detected when they start and end by double quotes
            if  match["token"].startswith('"') and match["token"].endswith('"'):
                # Remove the quote from the token
                token = match["token"].replace('"', '')
                # Store the token as an object
                objects += self.search(token)[:5]
            # Or if the previous word is a preposition
            elif is_preposition( previous_word(query, match["token"]) ):
                # Store the token as an object
                objects += self.search(match["token"])[:5]

        # No subject, no predicate, it might be a classic search
        if not len(subjects) and not len(predicates):
            results = self.search(query)
            for result in results:
                # Build the label
                label = result.get("name", None)
                propositions.append({
                    'label': label,
                    'subject': {
                        "name": result.get("id", None),
                        "label": label
                    },
                    'predicate': {
                        "label": "is instance of",
                        "name": "<<INSTANCE>>"
                    },
                    'object': result.get("model", None)
                })
        # We find some subjects
        elif len(subjects) and not len(predicates):
            rels = self.get_syntax().get("predicate").get("relationship")
            for subject in subjects:
                # Gets all available relationship for these subjects
                predicates += [ rel for rel in rels if rel["subject"] == subject["name"] ]

        # Add a default and irrelevant object
        if not len(objects): objects = [""]

        # Generate proposition using RDF's parts
        for subject in remove_duplicates(subjects):
            for predicate in remove_duplicates(predicates):
                for obj in objects:
                    pred_sub = predicate.get("subject", None)
                    # If the predicate has a subject
                    # and this matches to the current one
                    if pred_sub == None or pred_sub == subject.get("name", None):
                        if type(obj) is dict:
                            obj_disp = obj["name"] or obj["label"]
                        else:
                            obj_disp = obj
                        # Value to inset into the proposition's label
                        values = (subject["label"], predicate["label"], obj_disp,)
                        # Build the label
                        label = '%s that %s %s' % values
                        propositions.append({
                            'label'    : label,
                            'subject'  : subject,
                            'predicate': predicate,
                            'object'   : obj
                        })

        # Remove duplicates proposition dicts
        return propositions

    def get_syntax(self, bundle=None, request=None):
        return {
            'subject': {
                'model':  self.get_models_output(),
                'entity': None
            },
            'predicate': {
                'relationship': []
            }
        }
