import base64
import hashlib
import hmac
import json
from typing import List

import frappe
from frappe import _
from shopify.resources import Webhook
from shopify.session import Session

from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.shopify.constants import (
	SETTING_DOCTYPE,
	API_VERSION,
	WEBHOOK_EVENTS,
	EVENT_MAPPER,
)


def temp_shopify_session(func):
	def wrapper(*args, **kwargs):
		setting = frappe.get_doc(SETTING_DOCTYPE)
		auth_details = (setting.shopify_url, API_VERSION, setting.get_password("password"))

		with Session.temp(*auth_details):
			return func(*args, **kwargs)

	return wrapper


@temp_shopify_session
def register_webhooks() -> List[Webhook]:
	"""Register required webhooks with shopify and return registered webhooks."""
	new_webhooks = list()

	for topic in WEBHOOK_EVENTS:
		webhook = Webhook.create(
			{"topic": topic, "address": get_callback_url(), "format": "json"}
		)

		if webhook.is_valid():
			new_webhooks.append(webhook)
		else:
			create_shopify_log(
				status="Error",
				response_data=webhook.to_dict(),
				exception=webhook.errors.full_messages(),
			)

	return new_webhooks


@temp_shopify_session
def unregister_webhooks() -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""

	callback_url = get_callback_url()

	for webhook in Webhook.find():
		if webhook.address == callback_url:
			webhook.destroy()


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""

	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		url = frappe.conf.localtunnel_url
	else:
		url = frappe.request.host

	return f"https://{url}/api/method/ecommerce_integrations.shopify.connection.store_request_data"


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event)


def process_request(data, event):

	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"order": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = frappe.get_doc(SETTING_DOCTYPE)
	secret_key = settings.shared_secret

	sig = base64.b64encode(
		hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest()
	)

	if hmac_header and not sig == bytes(hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))