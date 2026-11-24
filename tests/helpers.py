"""Shared test helpers: build real dnspython responses and a scriptable fake network."""

from __future__ import annotations

import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rrset


def make_response(qname, rcode="NOERROR", answers=()):
    """Build a response to an A query.

    ``answers`` is a sequence of ``(owner, rdtype, [rdata, ...])`` tuples.
    """
    query = dns.message.make_query(qname, "A")
    response = dns.message.make_response(query)
    response.set_rcode(dns.rcode.from_text(rcode))
    response.flags |= dns.flags.RA
    for owner, rdtype, rdatas in answers:
        # Names decoded from a real wire response are always absolute; rrset.from_text
        # would otherwise build a relative owner name that never matches the qname.
        absolute_owner = dns.name.from_text(owner, dns.name.root)
        response.answer.append(dns.rrset.from_text(absolute_owner, 300, "IN", rdtype, *rdatas))
    return response


def a_response(qname, *ips):
    return make_response(qname, answers=[(qname, "A", list(ips))])


class FakeDNS:
    """Drop-in replacement for the network query function.

    ``handler(qname, nameserver, attempt)`` returns a response Message, or an
    exception instance which is raised. ``attempt`` is 0 for the first query of
    that name, 1 for the second, and so on.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []
        self._attempts: dict[str, int] = {}

    async def __call__(self, query, nameserver, timeout):
        qname = query.question[0].name.to_text().rstrip(".")
        attempt = self._attempts.get(qname, 0)
        self._attempts[qname] = attempt + 1
        self.calls.append((qname, nameserver))
        outcome = self.handler(qname, nameserver, attempt)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def count(self, qname):
        return sum(1 for name, _ in self.calls if name == qname)
