"""
The Alpaca paper adapter, and order idempotency.

The test that matters most here is: submit, response lost, retry, and
exactly ONE broker order exists afterwards. It is the mandatory gate for
any future live execution, because a retry after an uncertain response
that places a second order is the most expensive single failure an
automated trader has.

It is run under all three plausible duplicate policies, because Alpaca's
documentation does not say what a duplicate client_order_id returns. An
adapter that is only safe when the broker rejects duplicates is not
safe: it would be correct or ruinous depending on a fact nobody has
checked.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from agent.broker import BrokerAdapter, Quote                      # noqa: E402
from agent.broker.alpaca_paper import (                           # noqa: E402
    PAPER_BASE_URL, AlpacaPaperBroker, AlpacaPaperError, NotPaperEndpoint,
    RequestsTransport, UncertainSubmission, normalise_order,
)
from fake_alpaca import ACCEPT, REJECT, RETURN, FakeAlpaca        # noqa: E402

POLICIES = (REJECT, ACCEPT, RETURN)


def adapter(venue, clock=None, quarantine=180.0):
    now = [1000.0] if clock is None else clock
    return AlpacaPaperBroker(
        transport=venue, clock=lambda: now[0], sleep=lambda s: None,
        quarantine_seconds=quarantine), now


def submit(broker, cid="cid-1", qty=1.0):
    return broker.submit_order("XYZ", "BUY", qty, limit_price=100.5,
                               client_order_id=cid)


class TestPaperOnly(unittest.TestCase):
    """There must be no route to a live-money endpoint."""

    def test_the_default_base_url_is_the_paper_host(self):
        self.assertEqual(PAPER_BASE_URL, "https://paper-api.alpaca.markets")

    def test_a_live_base_url_is_refused(self):
        with self.assertRaises(NotPaperEndpoint):
            AlpacaPaperBroker(base_url="https://api.alpaca.markets")

    def test_a_lookalike_host_is_refused(self):
        """A suffix match would accept this."""
        with self.assertRaises(NotPaperEndpoint):
            AlpacaPaperBroker(
                base_url="https://paper-api.alpaca.markets.evil.example")

    def test_a_different_path_on_the_paper_host_is_refused(self):
        with self.assertRaises(NotPaperEndpoint):
            AlpacaPaperBroker(base_url="https://paper-api.alpaca.markets/x")

    def test_the_transport_rechecks_the_host_on_every_request(self):
        """
        Defence in depth: even if the constructor check were removed, the
        transport would still refuse a live URL.
        """
        transport = RequestsTransport("k", "s")
        with self.assertRaises(NotPaperEndpoint):
            transport.request("GET", "https://api.alpaca.markets/v2/account")

    def test_the_adapter_declares_itself_paper(self):
        self.assertTrue(adapter(FakeAlpaca())[0].capabilities()["is_paper"])

    def test_the_adapter_is_a_broker_adapter(self):
        self.assertIsInstance(adapter(FakeAlpaca())[0], BrokerAdapter)

    def test_no_base_url_is_read_from_the_environment(self):
        """Checked against the source: configuration cannot repoint it."""
        source = open(os.path.join(
            REPO_ROOT, "agent", "broker", "alpaca_paper.py")).read()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_no_live_host_appears_anywhere_in_the_adapter(self):
        source = open(os.path.join(
            REPO_ROOT, "agent", "broker", "alpaca_paper.py")).read()
        self.assertNotIn('"https://api.alpaca.markets', source)

    def test_credentials_never_appear_in_a_transport_error(self):
        """
        Some HTTP libraries echo request headers into exception text.
        Injects exactly that, and checks it does not survive into the
        error the adapter raises.

        Uses a stub `requests` module rather than the real one. The real
        one is not installed in every environment, and an earlier
        version skipped itself when it was missing - so a security check
        silently did not run, which reads as coverage and is not.
        """
        import types
        from unittest import mock
        from agent.broker.alpaca_paper import TransportError

        class Timeout(Exception):
            pass

        class ConnectionError(Exception):                     # noqa: A001
            pass

        leaking = ConnectionError(
            "connection failed; headers={'APCA-API-KEY-ID': "
            "'PKLEAKEDKEYID', 'APCA-API-SECRET-KEY': 'LEAKEDSECRETVALUE'}")

        def raise_leak(*args, **kwargs):
            raise leaking

        stub = types.ModuleType("requests")
        stub.Timeout, stub.ConnectionError = Timeout, ConnectionError
        stub.request = raise_leak

        transport = RequestsTransport("PKLEAKEDKEYID", "LEAKEDSECRETVALUE")
        with mock.patch.dict(sys.modules, {"requests": stub}):
            with self.assertRaises(TransportError) as caught:
                transport.request("GET", PAPER_BASE_URL + "/v2/account")
        text = str(caught.exception) + repr(caught.exception.__cause__)
        self.assertNotIn("PKLEAKEDKEYID", text)
        self.assertNotIn("LEAKEDSECRETVALUE", text)

    def test_falsifying_control_the_leak_check_can_fail(self):
        """The check above would pass if the exception never carried a
        secret at all; prove the injected one does."""
        self.assertIn("PKLEAKEDKEYID",
                      "headers={'APCA-API-KEY-ID': 'PKLEAKEDKEYID'}")


class TestCriticalIdempotency(unittest.TestCase):
    """submit -> response lost -> retry -> ONE order."""

    def _scenario(self, policy):
        venue = FakeAlpaca(duplicate_policy=policy)
        broker, _now = adapter(venue)
        venue.lose_response_after_processing = 1
        # First attempt: processed by the venue, reply lost.
        try:
            submit(broker)
            first_raised = False
        except UncertainSubmission:
            first_raised = True
        # Retry with the SAME deterministic id.
        retried = None
        try:
            retried = submit(broker)
        except UncertainSubmission:
            pass
        return venue, broker, first_raised, retried

    def test_a_lost_response_then_a_retry_places_exactly_one_order(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, _b, _f, _r = self._scenario(policy)
                self.assertEqual(
                    venue.order_count(), 1,
                    f"duplicate policy {policy}: the venue holds "
                    f"{venue.order_count()} orders after a lost response "
                    "and a retry")

    def test_the_lost_response_is_resolved_by_lookup_not_resubmission(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, _b, first_raised, _r = self._scenario(policy)
                self.assertEqual(venue.post_count, 1,
                                 "the order was POSTed more than once")
                self.assertFalse(first_raised,
                                 "a processed order should be found by "
                                 "lookup, not reported as uncertain")

    def test_the_resolved_order_is_the_real_one(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, _b, _f, retried = self._scenario(policy)
                [real] = list(venue.orders.values())
                self.assertEqual(retried["order_id"], real["id"])

    def test_the_position_is_not_doubled(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, broker, _f, _r = self._scenario(policy)
                self.assertAlmostEqual(
                    broker.get_position("XYZ")["quantity"], 1.0, places=6)

    def test_a_plain_retry_with_no_failure_places_one_order(self):
        """The same id twice, no fault at all."""
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                submit(broker)
                submit(broker)
                self.assertEqual(venue.order_count(), 1)

    def test_a_retry_after_a_cold_start_places_one_order(self):
        """
        A new adapter instance has no local memory. The broker is the
        memory, which is the whole reason to look up before posting.
        """
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                first, _ = adapter(venue)
                submit(first)
                second, _ = adapter(venue)            # cold start
                submit(second)
                self.assertEqual(venue.order_count(), 1)
                self.assertEqual(venue.post_count, 1)

    def test_five_retries_still_place_one_order(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                for _ in range(5):
                    submit(broker)
                self.assertEqual(venue.order_count(), 1)

    def test_distinct_ids_place_distinct_orders(self):
        """The falsifying control: dedup must not swallow real orders."""
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        submit(broker, "a")
        submit(broker, "b")
        self.assertEqual(venue.order_count(), 2)

    def test_the_id_is_required(self):
        broker, _ = adapter(FakeAlpaca())
        with self.assertRaises(AlpacaPaperError):
            broker.submit_order("XYZ", "BUY", 1.0, limit_price=100.0)


class TestTimeoutBeforeAcknowledgement(unittest.TestCase):
    """The request never reached the venue."""

    def test_the_order_is_not_found_and_the_id_is_quarantined(self):
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        venue.drop_before_processing = 1
        with self.assertRaises(UncertainSubmission):
            submit(broker)
        self.assertIn("cid-1", broker.quarantined())
        self.assertEqual(venue.order_count(), 0)

    def test_it_is_never_resubmitted_inside_the_quarantine(self):
        """
        "Not found yet" is not the same as "never sent": the request may
        still be in flight. Reposting would risk exactly the duplicate
        this mechanism exists to prevent.
        """
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                venue.drop_before_processing = 1
                with self.assertRaises(UncertainSubmission):
                    submit(broker)
                posts_before = venue.post_count
                with self.assertRaises(UncertainSubmission):
                    submit(broker)
                self.assertEqual(venue.post_count, posts_before)

    def test_it_may_be_submitted_after_the_quarantine_expires(self):
        """
        The id is not stranded forever: after the window, a lookup
        confirms absence and the order can be placed.
        """
        venue = FakeAlpaca()
        broker, now = adapter(venue, quarantine=180.0)
        venue.drop_before_processing = 1
        with self.assertRaises(UncertainSubmission):
            submit(broker)
        now[0] += 500.0
        broker._posted.discard("cid-1")
        order = submit(broker)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(venue.order_count(), 1)

    def test_a_late_arriving_original_is_found_not_duplicated(self):
        """
        The request was in flight, not lost. At resolution time the
        venue cannot yet show it, so the adapter has to quarantine; by
        the retry it has propagated, and the lookup must find it rather
        than post a second one.

        Modelled with a venue that hides the order from the first
        lookups. Under ACCEPT, where the venue would happily create a
        duplicate, a wrong answer here is a doubled position.
        """
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                venue.lose_response_after_processing = 1
                # Each lookup consumes two hidden reads (by-id plus the
                # list cross-check). The pre-POST lookup takes 2 and the
                # two resolve attempts take 4, so 6 hides the order from
                # all of them. An earlier 4 ran out before the second
                # resolve and the order was simply found - the setup, not
                # the adapter, was wrong.
                venue.hidden_lookups = 6

                with self.assertRaises(UncertainSubmission):
                    submit(broker)
                self.assertIn("cid-1", broker.quarantined())
                self.assertEqual(venue.order_count(), 1,
                                 "setup: the venue did process the order")

                # Propagation completes; the retry arrives.
                found = submit(broker)
                self.assertEqual(venue.order_count(), 1)
                self.assertEqual(venue.post_count, 1)
                self.assertEqual(found["status"], "FILLED")
                self.assertNotIn("cid-1", broker.quarantined(),
                                 "a resolved id should leave quarantine")

    def test_the_quarantine_survives_a_cold_start(self):
        """
        A Lambda dying mid-request is the scenario the quarantine exists
        for, so it has to be exportable and restorable.
        """
        venue = FakeAlpaca()
        first, now = adapter(venue)
        venue.drop_before_processing = 1
        with self.assertRaises(UncertainSubmission):
            submit(first)
        saved = first.export_state()

        second, _ = adapter(venue, clock=now)
        second.import_state(saved)
        posts = venue.post_count
        with self.assertRaises(UncertainSubmission):
            submit(second)
        self.assertEqual(venue.post_count, posts)


class TestTimeoutAfterAcknowledgement(unittest.TestCase):

    def test_a_server_error_after_processing_is_resolved_by_lookup(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                venue.fail_with_status_after_processing = 503
                order = submit(broker)
                self.assertEqual(order["status"], "FILLED")
                self.assertEqual(venue.order_count(), 1)
                self.assertEqual(venue.post_count, 1)

    def test_a_rate_limit_is_not_retried_blindly(self):
        """
        A rate-limited submission retried blindly is how one intended
        order becomes three.
        """
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue = FakeAlpaca(duplicate_policy=policy)
                broker, _ = adapter(venue)
                venue.fail_with_status = 429
                try:
                    submit(broker)
                except UncertainSubmission:
                    pass
                self.assertEqual(venue.post_count, 1)
                self.assertLessEqual(venue.order_count(), 1)

    def test_a_definite_rejection_is_reported_not_retried(self):
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        venue.fail_with_status = 403
        order = submit(broker)
        self.assertEqual(order["status"], "REJECTED")
        self.assertEqual(venue.post_count, 1)

    def test_a_definite_rejection_does_not_quarantine_the_id(self):
        """The venue said no, definitively. That is knowledge."""
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        venue.fail_with_status = 403
        submit(broker)
        self.assertNotIn("cid-1", broker.quarantined())


class TestOrderLifecycle(unittest.TestCase):

    def test_a_fill_is_normalised(self):
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        order = submit(broker)
        self.assertEqual(order["status"], "FILLED")
        self.assertAlmostEqual(order["filled_quantity"], 1.0, places=6)
        self.assertAlmostEqual(order["average_fill_price"], 100.0, places=4)

    def test_a_partial_fill_is_reported_as_partial(self):
        venue = FakeAlpaca()
        venue.fill_fraction = 0.4
        broker, _ = adapter(venue)
        order = submit(broker, qty=10.0)
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertAlmostEqual(order["filled_quantity"], 4.0, places=6)
        self.assertAlmostEqual(order["remaining_quantity"], 6.0, places=6)

    def test_a_venue_rejection_is_reported_as_rejected(self):
        venue = FakeAlpaca()
        venue.reject_next_order = "insufficient"
        broker, _ = adapter(venue)
        self.assertEqual(submit(broker)["status"], "REJECTED")

    def test_an_unrecognised_status_is_not_treated_as_terminal(self):
        """
        An order we cannot classify must not be assumed finished: that
        would stop managing something that may still be live.
        """
        raw = {"id": "o", "status": "some_new_status", "qty": "1",
               "filled_qty": "0", "symbol": "XYZ", "side": "buy"}
        self.assertEqual(normalise_order(raw)["status"], "PENDING")

    def test_a_cancel_is_confirmed_not_assumed(self):
        venue = FakeAlpaca()
        venue.leave_orders_open = True
        broker, _ = adapter(venue)
        order = submit(broker)
        result = broker.cancel_order(order["order_id"])
        self.assertEqual(result["status"], "CANCELLED")

    def test_a_cancel_race_reports_the_order_as_filled(self):
        """
        The order filled before the cancel landed. Treating the cancel
        request as success would leave a position the agent believes it
        cancelled.
        """
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        order = submit(broker)                       # fills immediately
        result = broker.cancel_order(order["order_id"])
        self.assertEqual(result["status"], "FILLED")
        self.assertGreater(result["filled_quantity"], 0.0)

    def test_replace_is_refused(self):
        broker, _ = adapter(FakeAlpaca())
        with self.assertRaises(AlpacaPaperError):
            broker.replace_order("x", quantity=2.0)

    def test_a_position_can_be_closed(self):
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        submit(broker)
        order = broker.close_position("XYZ")
        self.assertEqual(order["status"], "FILLED")
        self.assertIsNone(broker.get_position("XYZ"))

    def test_a_market_style_order_without_a_limit_is_refused(self):
        broker, _ = adapter(FakeAlpaca())
        with self.assertRaises(AlpacaPaperError):
            broker.submit_order("XYZ", "BUY", 1.0, client_order_id="x")

    def test_an_overlong_client_id_is_refused(self):
        broker, _ = adapter(FakeAlpaca())
        with self.assertRaises(AlpacaPaperError):
            submit(broker, cid="x" * 129)


class TestBrokerReadsFailLoudly(unittest.TestCase):

    def test_an_unreadable_broker_raises_rather_than_looking_empty(self):
        """
        An unreadable broker is an unknown position state. Returning []
        would make the agent believe it holds nothing.
        """
        from agent.broker.alpaca_paper import TransportError
        venue = FakeAlpaca()
        venue.unreadable = True
        broker, _ = adapter(venue)
        with self.assertRaises(TransportError):
            broker.get_positions()

    def test_a_failed_account_read_raises(self):
        class Failing:
            def request(self, *a, **k):
                from agent.broker.alpaca_paper import HttpResult
                return HttpResult(500, None)
        broker = AlpacaPaperBroker(transport=Failing())
        with self.assertRaises(AlpacaPaperError):
            broker.get_account()

    def test_an_external_position_is_visible(self):
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        submit(broker)
        self.assertEqual([p["symbol"] for p in broker.get_positions()],
                         ["XYZ"])

    def test_the_lookup_cross_checks_a_404_against_the_order_list(self):
        """
        A 404 from an endpoint that does not behave as assumed would
        otherwise read as "never placed", producing the duplicate.
        """
        venue = FakeAlpaca()
        broker, _ = adapter(venue)
        submit(broker)
        venue.lookup_endpoint_missing = True
        found = broker.find_by_client_order_id("cid-1")
        self.assertIsNotNone(found)

    def test_a_confirmed_absence_returns_none(self):
        """The falsifying control for the cross-check."""
        broker, _ = adapter(FakeAlpaca())
        self.assertIsNone(broker.find_by_client_order_id("nope"))


if __name__ == "__main__":
    unittest.main()
