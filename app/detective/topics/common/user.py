#!/usr/bin/env python
# -*- coding: utf-8 -*-
from .errors                      import *
from .message                     import Recover
from app.detective.utils          import topic_cache
from app.detective.models         import Topic, TopicToken, DetectiveProfileUser
from django.conf                  import settings
from django.conf.urls             import url
from django.contrib.auth          import authenticate, login, logout
from django.contrib.auth.models   import User, Group
from django.contrib.sites.models  import RequestSite
from django.core.paginator        import Paginator, InvalidPage
from django.core.exceptions       import PermissionDenied
from django.core                  import signing
from django.db.models             import Q
from django.db                    import IntegrityError
from django.middleware.csrf       import _get_new_csrf_key as get_new_csrf_key
from password_reset.views         import Reset
from registration.models          import RegistrationProfile, SHA1_RE
from tastypie                     import http, fields
from tastypie.authentication      import Authentication, SessionAuthentication, BasicAuthentication, MultiAuthentication
from tastypie.authorization       import ReadOnlyAuthorization
from tastypie.constants           import ALL, ALL_WITH_RELATIONS
from tastypie.resources           import ModelResource
from tastypie.validation          import Validation
from tastypie.utils               import trailing_slash
import hashlib
import random
import re

# basic dependencies between resources
# UserNestedResource -> GroupResource -> TopicResource -> UserResource
# TopicNestedResource -> UserNestedResource -> ProfileResource

class GroupResource(ModelResource):
    def getTopic(bundle):
        topic  = None
        module = bundle.obj.name.split("_")[0]
        try:
            topic = topic_cache.get_topic(module)
        except Topic.DoesNotExist: pass
        return topic

    topic = fields.ToOneField('app.detective.topics.common.resources.TopicResource',
                                attribute=getTopic,
                                use_in='detail',
                                null=True,
                                full=True)
    class Meta:
        excludes = ['topic',]
        queryset = Group.objects.all()
        filtering = { 'name' : ALL }

class UserAuthorization(ReadOnlyAuthorization):
    def update_detail(self, object_list, bundle):
        authorized = False
        if bundle.request:
            __user = bundle.obj.user if hasattr(bundle.obj, 'user') else bundle.obj
            authorized = ((__user == bundle.request.user) or bundle.request.user.is_staff)
        return authorized

    def delete_detail(self, object_list, bundle):
        authorized = False
        if bundle.request:
            authorized = ((bundle.obj == bundle.request.user) or bundle.request.user.is_staff)
        return authorized

class ProfileResource(ModelResource):
    avatar       = fields.CharField(   attribute='avatar'      , readonly=True)
    topics_max   = fields.IntegerField(attribute='topics_max'  , readonly=True)
    topics_count = fields.IntegerField(attribute='topics_count', readonly=True)
    nodes_max    = fields.IntegerField(attribute='nodes_max'   , readonly=True)
    # NOTE: Very expensive if cache is disabled
    nodes_count  = fields.DictField(   attribute='nodes_count' , readonly=True)

    class Meta:
        authentication     = MultiAuthentication(Authentication(), SessionAuthentication(), BasicAuthentication())
        authorization      = UserAuthorization()
        always_return_data = True
        queryset           = DetectiveProfileUser.objects.all()
        resource_name      = 'profile'
        allowed_methods    = ['get', 'patch']
        fields             = ['id', 'location', 'organization', 'url', 'avatar', 'plan', 'topics_count', 'topics_max', 'nodes_max', 'nodes_count']


class UserNestedResource(ModelResource):
    profile = fields.ToOneField(ProfileResource, 'detectiveprofileuser', full=True, null=True)

    class Meta:
        resource_name      = 'user'
        authentication     = MultiAuthentication(Authentication(), SessionAuthentication(), BasicAuthentication())
        authorization      = UserAuthorization()
        allowed_methods    = ['get', 'post', 'delete']
        always_return_data = True
        fields             = ['id', 'first_name', 'last_name', 'username', 'email', 'is_staff', 'password', 'profile']
        filtering          = {'id': ALL, 'username': ALL, 'email': ALL}
        queryset           = User.objects.all()
        resource_name      = 'user'

    def prepend_urls(self):
        params = (self._meta.resource_name, trailing_slash())
        return [
            url(r"^(?P<resource_name>%s)/login%s$"                    % params, self.wrap_view('login'),                  name="api_login"),
            url(r'^(?P<resource_name>%s)/logout%s$'                   % params, self.wrap_view('logout'),                 name='api_logout'),
            url(r'^(?P<resource_name>%s)/status%s$'                   % params, self.wrap_view('status'),                 name='api_status'),
            url(r'^(?P<resource_name>%s)/permissions%s$'              % params, self.wrap_view('permissions'),            name='api_user_permissions'),
            url(r'^(?P<resource_name>%s)/me%s$'                       % params, self.wrap_view('me'),                     name='api_user_me'),
            url(r'^(?P<resource_name>%s)/signup%s$'                   % params, self.wrap_view('signup'),                 name='api_signup'),
            url(r'^(?P<resource_name>%s)/activate%s$'                 % params, self.wrap_view('activate'),               name='api_activate'),
            url(r'^(?P<resource_name>%s)/change_password%s$'          % params, self.wrap_view('change_password'),        name='api_change_password'),
            url(r'^(?P<resource_name>%s)/reset_password%s$'           % params, self.wrap_view('reset_password'),         name='api_reset_password'),
            url(r'^(?P<resource_name>%s)/reset_password_confirm%s$'   % params, self.wrap_view('reset_password_confirm'), name='api_reset_password_confirm'),
            url(r"^(?P<resource_name>%s)/(?P<pk>\w[\w/-]*)/groups%s$" % params, self.wrap_view('get_groups'),             name="api_get_groups"),
        ]

    def login(self, request, **kwargs):
        self.method_check(request, allowed=['post'])

        data = self.deserialize(request, request.body, format=request.META.get('CONTENT_TYPE', 'application/json'))

        username    = data.get('username', '')
        password    = data.get('password', '')
        remember_me = data.get('remember_me', False)

        if username == '' or password == '':
            return self.create_response(request, {
                'success': False,
                'error_message': 'Missing username or password',
            })

        user = authenticate(username=username, password=password)
        if user:
            if user.is_active:
                login(request, user)

                # Remember me opt-in
                if not remember_me: request.session.set_expiry(0)
                response = self.create_response(request, {
                    'success' : True,
                    'is_staff': user.is_staff,
                    'permissions': list(user.get_all_permissions()),
                    'username': user.username
                })
                # Create CSRF token
                response.set_cookie("csrftoken", get_new_csrf_key())

                return response
            elif not user.is_active:
                return self.create_response(request, {
                    'success': False,
                    'error_message': 'Account not activated yet.',
                })
            else:
                return self.create_response(request, {
                    'success': False,
                    'error_message': 'Account activated but not authorized yet.',
                })
        else:
            return self.create_response(request, {
                'success': False,
                'error_message': 'Incorrect password or username.',
                'error_code': 'incorrect_password_or_email'
            })

    def dehydrate(self, bundle):
        bundle.data["email"]    = u"☘"
        bundle.data["password"] = u"☘"
        return bundle

    def hydrate(self, bundle):
        # Make sure we do not send "☘" in the database
        bundle.data["email"] = None
        bundle.data["password"] = None
        return bundle

    def get_activation_key(self, username=""):
        salt = hashlib.sha1(str(random.random())).hexdigest()[:5]
        if isinstance(username, unicode):
            username = username.encode('utf-8')
        return hashlib.sha1(salt+username).hexdigest()

    def signup(self, request, **kwargs):
        self.method_check(request, allowed=['post'])
        data = self.deserialize(request, request.body, format=request.META.get('CONTENT_TYPE', 'application/json'))
        try:
            self.validate_request(data, ['username', 'email', 'password'])
            user = User.objects.create_user(
                data.get("username"),
                data.get("email"),
                data.get("password")
            )
            # Create an inactive user
            setattr(user, "is_active", False)
            user.save()
            # User used a invitation token
            if data.get("token", None) is not None:
                try:
                    topicToken = TopicToken.objects.get(token=data.get("token"))
                    # Add the user to the topic contributor group
                    topicToken.topic.get_contributor_group().user_set.add(user)
                    # Remove the token
                    topicToken.delete()
                except TopicToken.DoesNotExist:
                    # Failed silently if the token is unkown
                    pass
            # Could we activate the new account by email?
            if settings.ACCOUNT_ACTIVATION_ENABLED:
                # Creates the activation key
                activation_key = self.get_activation_key(user.username)
                # Create the regisration profile
                rp = RegistrationProfile.objects.create(user=user, activation_key=activation_key)
                # Send the activation email
                rp.send_activation_email( RequestSite(request) )
            # Output the answer
            return http.HttpCreated()
        except MalformedRequestError as e:
            return http.HttpBadRequest(e.message)
        except PermissionDenied as e:
            return http.HttpForbidden(e.message)
        except IntegrityError as e:
            return http.HttpForbidden("%s in request payload (JSON)" % e)

    def activate(self, request, **kwargs):
        try:
            self.validate_request(request.GET, ['token'])
            token = request.GET.get("token", None)
            # Make sure the key we're trying conforms to the pattern of a
            # SHA1 hash; if it doesn't, no point trying to look it up in
            # the database.
            if SHA1_RE.search(token):
                profile = RegistrationProfile.objects.get(activation_key=token)
                if not profile.activation_key_expired():
                    user = profile.user
                    user.is_active = True
                    user.save()
                    profile.activation_key = RegistrationProfile.ACTIVATED
                    profile.save()
                    return self.create_response(request, {
                            "success": True
                        })
                else:
                    return http.HttpForbidden('Your activation token is no longer active or valid')
            else:
                return http.HttpForbidden('Your activation token  is no longer active or valid')

        except RegistrationProfile.DoesNotExist:
            return http.HttpNotFound('Your activation token is no longer active or valid')

        except MalformedRequestError as e:
            return http.HttpBadRequest("%s as request GET parameters" % e)

    def logout(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        if request.user and request.user.is_authenticated():
            logout(request)
            return self.create_response(request, { 'success': True })
        else:
            return self.create_response(request, { 'success': False })

    def status(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        if request.user and request.user.is_authenticated():
            return self.create_response(request, { 'is_logged': True,  'username': request.user.username })
        else:
            return self.create_response(request, { 'is_logged': False, 'username': '' })

    def permissions(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        self.is_authenticated(request)
        if request.user.is_authenticated():
            # Get the list of permission and sorts it alphabeticly
            permissions = list(request.user.get_all_permissions())
            permissions.sort()
            return self.create_response(request, {
                'permissions': permissions
            })
        else:
            return http.HttpUnauthorized('You need to be logged to list your permissions')

    def me(self, request, **kwargs):
        self.method_check(request, allowed=['get'])
        self.is_authenticated(request)
        if request.user.is_authenticated():
            bundle = self.build_bundle(obj=request.user, request=request)
            bundle = self.full_dehydrate(bundle)
            # Get the list of permission and sorts it alphabeticly
            permissions = list(request.user.get_all_permissions())
            permissions.sort()
            # Add user's permissions
            bundle.data["permissions"] = permissions
            return self.create_response(request, bundle)
        else:
            return http.HttpUnauthorized('You need to be logged.')

    def reset_password(self, request, **kwargs):
        """
        Send the reset password email to user with the proper URL.
        """
        self.method_check(request, allowed=['post'])
        data = self.deserialize(request, request.body, format=request.META.get('CONTENT_TYPE', 'application/json'))
        try:
            # Email required
            if not "email" in data or data['email'] == '': 
                return http.HttpBadRequest('Missing email')
            email   = data['email']
            user    = User.objects.get(email=email)
            recover = Recover()
            recover.user = user
            recover.request = request
            recover.email_template_name = 'email.reset-password.txt'
            recover.email_subject_template_name = 'email.reset-password.subject.txt'
            recover.send_notification()
            return self.create_response(request, { 'success': True })
        except User.DoesNotExist:
            message = 'The specified email (%s) doesn\'t match with any user' % email
            return http.HttpNotFound(message)
        except MalformedRequestError as error:
            return http.HttpBadRequest("%s in request payload (JSON)" % error)

    def reset_password_confirm(self, request, **kwargs):
        """
        Reset the password if the POST's token parameter is a valid token
        """
        self.method_check(request, allowed=['post'])
        reset = Reset()
        data  = self.deserialize(
                    request,
                    request.body,
                    format=request.META.get('CONTENT_TYPE', 'application/json')
                )
        try:
            self.validate_request(data, ['token', 'password'])
            tok          = data['token']
            raw_password = data['password']
            pk = signing.loads(tok, max_age=reset.token_expires,salt=reset.salt)
            user = User.objects.get(pk=pk)
            user.set_password(raw_password)
            user.save()
            return self.create_response(request, { 'success': True })
        except signing.BadSignature:
            return http.HttpForbidden('Wrong signature, your token may had expired (valid for 48 hours).')
        except MalformedRequestError as e:
            return http.HttpBadRequest(e)

    def change_password(self, request, **kwargs):
        self.method_check(request, ['post'])
        self.is_authenticated(request)
        data  = self.deserialize(
                    request,
                    request.body,
                    format=request.META.get('CONTENT_TYPE', 'application/json')
                )
        try:
            self.validate_request(data, ['new_password'])
            new_password = data['new_password']
            user         = request.user
            user.set_password(new_password)
            user.save()
            return self.create_response(request, {'success': True})
        except MalformedRequestError as e:
            return http.HttpBadRequest(e)

    def validate_request(self, data, fields):
        """
        Validate passed `data` based on the required `fields`.
        """
        missing_fields = []
        for field in fields:
            if field not in data.keys() or data[field] is None or data[field] == "":
                missing_fields.append(field)

        if len(missing_fields) > 0:
            message = "Malformed request. The following fields are required: %s" % ', '.join(missing_fields)
            raise MalformedRequestError(message)

        if 'email' in fields:
            email = data['email']
            if User.objects.filter(email__iexact=email).count():
                raise PermissionDenied("Email must be unique.")

        if 'username' in fields:
            username = data['username']
            # match only a-z, A-Z, 0-9 and [@,+,-,_,.] chars
            pattern  = re.compile("^([\w\.\-])+$")
            matching = re.match(pattern, username)
            if not matching:
                message = "Nice touch, but you can only use numbers, letters, and @, -, _ or . for your username"
                raise MalformedRequestError(message)

    def get_groups(self, request, **kwargs):
        # from app.detective.utils import dumb_profiler
        # import time
        # dumb_profiler.dispatch_time = time.time()
        # db_start = time.time()

        self.method_check(request, allowed=['get'])
        self.is_authenticated(request)
        self.throttle_check(request)

        try:
            bundle = self.build_bundle(data={'pk': kwargs['pk']}, request=request)
            obj = self.cached_obj_get(bundle=bundle, **self.remove_api_resource_names(kwargs))
        except User.DoesNotExist:
            return http.HttpNotFound("User not found")
        user = bundle.request.user
        group_resource = GroupResource()
        groups = group_resource.obj_get_list(bundle).filter(Q(user__id=obj.id)).exclude(topic__pk=None)
        if not user or not user.is_staff:
            if user:
                read_perms = [perm.split('.')[0] for perm in user.get_all_permissions() if perm.endswith(".contribute_read")]
                read_perms = [perm for vector in [[perm + '_contributor', perm + '_administrator'] for perm in read_perms] for perm in vector]
                q_filter = Q(topic__public=True) | Q(name__in=read_perms)
            else:
                q_filter = Q(topic__public=True)
            groups = groups.filter(q_filter)

        # dumb_profiler.db_time = time.time() - db_start

        limit     = int(request.GET.get('limit', 20))
        paginator = Paginator(groups, limit)
        objects   = []

        # serializer_start = time.time()

        try:
            p    = int(request.GET.get('page', 1))
            page = paginator.page(p)

            for group in page.object_list:
                bundle = group_resource.build_bundle(obj=group, request=request)
                bundle = group_resource.full_dehydrate(bundle)
                objects.append(bundle)

        except InvalidPage:
            # Allow empty page
            pass

        object_list = {
            'objects': objects,
            'meta': {
                'page': p,
                'limit': limit,
                'total_count': paginator.count
            }
        }

        response = group_resource.create_response(request, object_list)
        # dumb_profiler.serializer_time = time.time() - serializer_start
        return response

class UserResource(UserNestedResource):
    class Meta(UserNestedResource.Meta):
        resource_name = 'user-simpler'
        # we remove profile from fields
        fields = ['id', 'first_name', 'last_name', 'username', 'email', 'is_staff', 'password',]
