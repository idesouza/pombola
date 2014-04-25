import hashlib
import json
from random import randint, shuffle
import re
import sys

from django.conf import settings
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.utils.http import urlquote
from django.views.generic.base import TemplateView, RedirectView
from django.views.generic.edit import FormView

from .forms import CountyPerformancePetitionForm, CountyPerformanceSenateForm

from django.shortcuts import redirect

from pombola.feedback.models import Feedback
from pombola.experiments.models import Experiment, Event


def sanitize_parameter(key, parameters, allowed_values, default_value=None):
    value = parameters.get(key)
    if value not in allowed_values:
        if default_value is None:
            message = "An allowed value for '{0}' must be provided"
            raise ValueError(message.format(key))
        value = default_value
    return value

user_key_re = re.compile(r'^[a-zA-Z0-9]+$')

def sanitize_user_key(parameters):
    if 'user_key' in parameters and user_key_re.search(parameters['user_key']):
        return parameters['user_key']
    return '?'

def sanitize_data_parameters(request, parameters):
    result = {}
    result['variant'] = sanitize_parameter(
        key='variant',
        parameters=parameters,
        allowed_values=('o', 't', 'n'),
        default_value='n')
    result['g'] = sanitize_parameter(
        key='g',
        parameters=parameters,
        allowed_values=('m', 'f'),
        default_value='?')
    result['agroup'] = sanitize_parameter(
        key='agroup',
        parameters=parameters,
        allowed_values=('under', 'over'),
        default_value='?')
    result['user_key'] = sanitize_user_key(parameters)
    return result


class CountyPerformanceView(TemplateView):
    """This view displays a page about county performance with calls to action

    There are some elements of the page that are supposed to be
    randomly ordered.  There are also three major variants of the
    page that include different information."""

    template_name = 'county-performance.html'

    def get_context_data(self, **kwargs):
        context = super(CountyPerformanceView, self).get_context_data(**kwargs)
        context['suppress_banner'] = True
        context['petition_form'] = CountyPerformancePetitionForm()
        context['senate_form'] = CountyPerformanceSenateForm()

        context.update(sanitize_data_parameters(self.request, self.request.GET))

        # Note that this has to come after updating the context:
        context['user_key'] = str(randint(0, sys.maxint))

        context['show_opportunity'], context['show_threat'] = {
            'o': (True, False),
            't': (False, True),
            'n': (False, False),
            None: (False, False),
        }[context['variant']]

        context['share_partials'] = [
            '_share_twitter.html',
            '_share_facebook.html',
        ]
        shuffle(context['share_partials'])

        context['major_partials'] = [
            '_county_share.html',
            '_county_petition.html',
            '_county_senate.html',
        ]
        shuffle(context['major_partials'])

        return context


class CountyPerformanceDataMixin(object):

    def get_extra_data(self, sanitized_data):
        return {
            'gender': sanitized_data['g'],
            'age_group': sanitized_data['agroup']
        }

    def get_event_parameters(self, sanitized_data):
        return {
            'user_key': sanitized_data['user_key'],
            'variant': sanitized_data['variant'],
        }

    def create_feedback(self, form, comment='', email=''):
        """A helper method for adding feedback to the database"""
        feedback = Feedback()
        feedback.status = 'non-actionable'
        prefix_data = self.get_extra_data(form.cleaned_data)
        prefix_data.update(self.get_event_parameters(form.cleaned_data))
        comment_prefix = json.dumps(prefix_data)
        feedback.comment = comment_prefix + ' ' + comment
        feedback.email = email
        feedback.url = self.request.build_absolute_uri()
        if self.request.user.is_authenticated():
            feedback.user = self.request.user
        feedback.save()

    def create_event(self, data, event_parameters=None):
        event_kwargs = self.get_event_parameters(data)
        if event_parameters is not None:
            event_kwargs.update(event_parameters)
        event_kwargs['extra_data'] = json.dumps(self.get_extra_data(data))
        experiment = Experiment.objects.get(slug='mit-county')
        experiment.event_set.create(**event_kwargs)


class CountyPerformanceSubmissionMixin(CountyPerformanceDataMixin):
    """A mixin useful for handling senate comment and petition emails"""

    def form_invalid(self, form):
        """Redirect back to a reduced version of the page from either form"""
        extra_context = {
            '{0}_form'.format(self.form_key): form,
            'major_partials': ['_county_{0}.html'.format(self.form_key)],
            'suppress_banner': True,
            'correct_errors': True}
        context = self.get_context_data(**extra_context)
        return self.render_to_response(context)

    def form_valid(self, form):
        self.create_feedback_from_form(form)
        self.create_event(form.cleaned_data,
                          {'category': 'form',
                           'action': 'submit',
                           'label': self.form_key})
        return super(CountyPerformanceSubmissionMixin,
                     self).form_valid(form)


class CountyPerformanceSenateSubmission(CountyPerformanceSubmissionMixin,
                                        FormView):
    """A view for handling submissions of comments for the senate"""

    template_name = 'county-performance.html'
    success_url = '/county-performance/senate/thanks'
    form_class = CountyPerformanceSenateForm
    form_key = 'senate'

    def create_feedback_from_form(self, form):
        new_comment = form.cleaned_data.get('comments', '').strip()
        self.create_feedback(form, comment=new_comment)


class CountyPerformancePetitionSubmission(CountyPerformanceSubmissionMixin,
                                          FormView):
    """A view for handling a petition signature"""

    template_name = 'county-performance.html'
    success_url = '/county-performance/petition/thanks'
    form_class = CountyPerformancePetitionForm
    form_key = 'petition'

    def create_feedback_from_form(self, form):
        new_comment = form.cleaned_data.get('name', '').strip()
        self.create_feedback(form,
                             comment=new_comment,
                             email=form.cleaned_data.get('email'))


class CountyPerformanceShare(CountyPerformanceDataMixin, RedirectView):
    """For recording & enacting Facebook / Twitter share actions"""

    def get_redirect_url(self, *args, **kwargs):
        social_network = sanitize_parameter(
            key='n',
            parameters=self.request.GET,
            allowed_values=('facebook', 'twitter'))
        data = sanitize_data_parameters(self.request, self.request.GET)
        self.create_event(data,
                          {'category': 'share-click',
                           'action': 'click',
                           'label': social_network})
        path = reverse('county-performance')
        built = self.request.build_absolute_uri(path)
        url_parameter = urlquote(built, safe='')
        url_formats = {
            'facebook': "https://www.facebook.com/sharer/sharer.php?u={0}",
            'twitter': "http://twitter.com/share?url={0}"}
        return url_formats[social_network].format(url_parameter)
