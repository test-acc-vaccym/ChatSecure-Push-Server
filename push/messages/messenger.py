import collections
import logging

import datetime
from copy import deepcopy

from push_notifications.apns import apns_send_bulk_message, apns_send_message
from push_notifications.gcm import send_bulk_message as gcm_send_bulk_message, send_message as gcm_send_message

from analytics import analytics
from analytics.events import SEND_PUSH_MESSAGE
from push.celery import app
from django.conf import settings
from push_notifications.conf import get_manager

USE_MESSAGE_QUEUE = settings.CHATSECURE_PUSH['USE_MESSAGE_QUEUE']
DATE_FORMAT = '%Y-%m-%dT%H:%M:%S.%f'  # Used to marshal enqueue date to celery

logger = logging.getLogger("django")


def send_apns(registration_ids, message, priority, **kwargs):
    apns_message = deepcopy(message)

    if USE_MESSAGE_QUEUE:
        _task_send_apns.delay(registration_ids, apns_message, **dict(kwargs, enqueue_date=datetime.datetime.utcnow().strftime(DATE_FORMAT)))
    else:
        _send_apns(registration_ids, apns_message, **kwargs)

    if priority == 'high':
        foreground_message = deepcopy(message)
        foreground_message['body'] = 'New Message!'
        foreground_message['loc-key'] = 'New Message!'
        foreground_message['thread_id'] = 'New Message!'
        # foreground_message['sound'] = 'default'
        # foreground_message['type'] = alert_type
        # foreground_message['collapse_id'] = alert_type
        new_kwargs = dict(kwargs, enqueue_date=datetime.datetime.utcnow().strftime(DATE_FORMAT), sound='default')
        if USE_MESSAGE_QUEUE:
            _task_send_apns.delay(registration_ids, foreground_message, **new_kwargs)
        else:
            _send_apns(registration_ids, foreground_message, **new_kwargs)



def send_gcm(registration_ids, message, **kwargs):
    if USE_MESSAGE_QUEUE:
        _task_send_gcm.delay(registration_ids, message, **dict(kwargs, enqueue_date=datetime.datetime.utcnow().strftime(DATE_FORMAT)))
    else:
        _send_gcm(registration_ids, message, **kwargs)


def _send_apns(registration_ids, message, **kwargs):
    '''
    Send a message to one or more APNS recipients

    :param registration_ids: a single or iterable collection of registration ids (APNS tokens)
    :param message: the payload to send. This is sent as the value of the 'alert' APNS key
    :param kwargs: additional APNS arguments. See push_notifications.apns._apns_sendd
    '''

    # Strip whitespace from APNS Registration Ids. This is also done on ingestion for new registration_ids
    registration_ids = [reg_id.replace(" ", "") for reg_id in registration_ids]

    enqueue_date_str = kwargs.pop('enqueue_date', None)

    priority = 'low'
    if message.get('body', None) is not None:
        priority = 'high'

    try:
        if isinstance(registration_ids, collections.Iterable):
            apns_send_bulk_message(registration_ids, message, **kwargs)
        else:
            apns_send_message(registration_ids, message, **kwargs)
        log_message_sent(enqueue_date_str=enqueue_date_str, priority=priority)
    except Exception as exception:
        logger.exception("Exception sending APNS message. %s : %s" % (exception.__class__.__name__, str(exception)))

        # We log a 'message sent with exception' event as well as the full exception itself
        log_message_sent(exception=exception, enqueue_date_str=enqueue_date_str, priority=priority)
        analytics.exception()


def _send_gcm(registration_ids, message, **kwargs):
    '''
    Send a message to one or more GCM recipients

    :param registration_ids: a single or iterable collection of registraion ids (GCM tokens)
    :param message: the payload to send. This is sent as the value of the 'message' GCM key,
    itself within the 'extra' key.
    :param kwargs: additional GCM arguments. Currently inserted directly into the payload
    '''

    data = kwargs.pop("extra", {})
    enqueue_date_str = kwargs.pop('enqueue_date', None)

    if message is not None:
        data["message"] = message

    if isinstance(registration_ids, collections.Iterable):
        gcm_send_bulk_message(registration_ids, data, **kwargs)
    else:
        gcm_send_message(registration_ids, data, **kwargs)

    log_message_sent(enqueue_date_str=enqueue_date_str)


@app.task(ignore_result=True)
def _task_send_apns(registration_ids, message, **kwargs):
    setup_rollbar()
    return _send_apns(registration_ids, message, **kwargs)


@app.task(ignore_result=True)
def _task_send_gcm(registration_ids, message, **kwargs):
    setup_rollbar()
    return _send_gcm(registration_ids, message, **kwargs)


def setup_rollbar():
    """
    Setup Rollbar uncaught exception handling. The Django application is automatically setup via Rollbar's middleware,
    but worker processes need to explicitly setup rollbar.
    """
    import rollbar
    import os

    rollbar.init(os.environ.get('ROLLBAR_ACCESS_TOKEN', ''), 'development' if settings.DEBUG else 'production')


def log_message_sent(exception=None, priority=None, enqueue_date_str=None):
    extra_data = {
        'using_sandbox': get_manager().get_apns_use_sandbox(None)
    }
    if priority is not None:
        extra_data['priority'] = priority

    if enqueue_date_str is not None:
        now = datetime.datetime.utcnow()
        enqueue_date = datetime.datetime.strptime(enqueue_date_str, DATE_FORMAT)

        queue_time_s = (now - enqueue_date).total_seconds()
        extra_data['queue_time_s'] = queue_time_s

    if exception is not None:
        extra_data['exception'] = "%s : %s" % (exception.__class__.__name__, str(exception))

    analytics.event(SEND_PUSH_MESSAGE, extra_data)
