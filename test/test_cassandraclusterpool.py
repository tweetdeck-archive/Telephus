from __future__ import with_statement

import random
import contextlib
from twisted.trial import unittest
from twisted.internet import defer, reactor
from telephus.pool import (CassandraClusterPool, CassandraPoolReconnectorFactory,
                           CassandraPoolParticipantClient)
from telephus.cassandra import Cassandra, constants
from telephus.cassandra.ttypes import *
from Cassanova import cassanova

def deferwait(s, result=None):
    def canceller(my_d):
        dcall.cancel()
    d = defer.Deferred(canceller=canceller)
    dcall = reactor.callLater(s, d.callback, result)
    return d

class CassandraClusterPoolTest(unittest.TestCase):
    start_port = 44449
    ksname = 'TestKeyspace'

    def assertFired(self, d):
        self.assert_(d.called, msg='%s has not been fired' % (d,))

    @contextlib.contextmanager
    def cluster_and_pool(self, num_nodes=10, pool_size=5, start=True):
        cluster = FakeCassandraCluster(num_nodes, start_port=self.start_port)
        pool = CassandraClusterPool([cluster.iface], thrift_port=self.start_port,
                                    pool_size=pool_size)
        if start:
            cluster.startService()
            pool.startService()
        self.cluster = cluster
        self.pool = pool
        try:
            yield cluster, pool
        finally:
            del self.pool
            del self.cluster
            if pool.running:
                pool.stopService()
            if cluster.running:
                cluster.stopService()

    @defer.inlineCallbacks
    def make_standard_cfs(self, ksname=None):
        if ksname is None:
            ksname = self.ksname
        yield self.pool.system_add_keyspace(
            KsDef(
                name=ksname,
                replication_factor=1,
                strategy_class='org.apache.cassandra.locator.SimpleStrategy',
                cf_defs=(
                    CfDef(
                        keyspace=ksname,
                        name='Standard1',
                        column_type='Standard'
                    ),
                    CfDef(
                        keyspace=ksname,
                        name='Super1',
                        column_type='Super'
                    )
                )
            )
        )
        yield self.pool.set_keyspace(ksname)
        yield self.pool.insert('key', 'Standard1', column='col', value='value')

    @defer.inlineCallbacks
    def insert_dumb_rows(self, ksname=None, cf=None, numkeys=10, numcols=10,
                         timestamp=0):
        if ksname is None:
            ksname = self.ksname
        if cf is None:
            cf = 'Standard1'
        yield self.pool.set_keyspace(ksname)

        mutmap = {}
        for k in range(numkeys):
            key = 'key%03d' % k
            cols = [Column(name='%s-%03d-%03d' % (ksname, k, n),
                           value='val-%s-%03d-%03d' % (ksname, k, n),
                           timestamp=timestamp)
                    for n in range(numcols)]
            mutmap[key] = {cf: cols}
        yield self.pool.batch_mutate(mutationmap=mutmap)

    @defer.inlineCallbacks
    def test_set_keyspace(self):
        pool_size=10
        num_nodes=4

        with self.cluster_and_pool(num_nodes=num_nodes, pool_size=pool_size):
            yield self.make_standard_cfs('KS1')
            yield self.make_standard_cfs('KS2')

            yield self.insert_dumb_rows('KS1', numcols=pool_size+2)
            yield self.insert_dumb_rows('KS2', numcols=pool_size+2)

            yield self.pool.set_keyspace('KS1')
            first = self.pool.get('key000', 'Standard1/wait=2.0', 'KS1-000-000')

            yield self.pool.set_keyspace('KS2')
            dfrds1 = []
            for x in range(pool_size + 1):
                d = self.pool.get('key001', 'Standard1/wait=0.1', 'KS2-001-%03d' % x)
                dfrds1.append(d)

            # all pool connections should have sent a real set_keyspace by
            # now; change it up again

            yield self.pool.set_keyspace('KS1')
            dfrds2 = []
            for x in range(pool_size + 1):
                d = self.pool.get('key002', 'Standard1/wait=0.1', 'KS1-002-%03d' % x)
                dfrds2.append(d)

            result = yield defer.DeferredList(dfrds1, consumeErrors=True)
            for n, (succ, res) in enumerate(result):
                self.assert_(succ, 'Failure on item %d was %s' % (n, res))
                res = res.column.value
                self.assertEqual(res, 'val-KS2-001-%03d' % n)

            result = yield defer.DeferredList(dfrds2)
            for n, (succ, res) in enumerate(result):
                self.assert_(succ, 'Failure was %s' % res)
                res = res.column.value
                self.assertEqual(res, 'val-KS1-002-%03d' % n)

            yield self.pool.set_keyspace('KS2')

            result = (yield first).column.value
            self.assertEqual(result, 'val-KS1-000-000')

            final = yield self.pool.get('key003', 'Standard1', 'KS2-003-005')
            self.assertEqual(final.column.value, 'val-KS2-003-005')

    @defer.inlineCallbacks
    def test_bad_set_keyspace(self):
        with self.cluster_and_pool():
            yield self.make_standard_cfs('KS1')
            yield self.insert_dumb_rows('KS1')

            yield self.assertFailure(self.pool.set_keyspace('i-dont-exist'),
                                     InvalidRequestException)
            self.flushLoggedErrors()

            # should still be in KS1
            result = yield self.pool.get('key005', 'Standard1', 'KS1-005-000')
            self.assertEqual(result.column.value, 'val-KS1-005-000')

    @defer.inlineCallbacks
    def test_ring_inspection(self):
        with self.cluster_and_pool(start=False):
            self.assertEqual(len(self.pool.seed_list), 1)
            self.cluster.startService()
            self.pool.startService()
            yield self.pool.describe_cluster_name()
            self.assertEqual(len(self.pool.nodes), len(self.cluster.ring), msg=str(sorted(self.pool.nodes)))

    @defer.inlineCallbacks
    def test_keyspace_connection(self):
        numkeys = 10
        numcols = 10
        tries = 500

        with self.cluster_and_pool():
            yield self.make_standard_cfs('KS1')
            yield self.make_standard_cfs('KS2')
            yield self.insert_dumb_rows('KS1', numkeys=numkeys, numcols=numcols)
            yield self.insert_dumb_rows('KS2', numkeys=numkeys, numcols=numcols)

            ksconns = dict((ksname, self.pool.keyspaceConnection(ksname))
                           for ksname in ('KS1', 'KS2'))

            dlist = []
            for i in xrange(tries):
                keyspace = 'KS%d' % random.randint(1, 2)
                keynum = '%03d' % random.randint(0, numkeys-1)
                key = 'key' + keynum
                col = '%s-%s-%03d' % (keyspace, keynum, random.randint(0, numcols-1))
                d = ksconns[keyspace].get(key, 'Standard1', col)
                d.addCallback(lambda c: c.column.value)
                d.addCallback(self.assertEqual, 'val-' + col)
                dlist.append(d)
            results = yield defer.DeferredList(dlist, consumeErrors=True)
            for succ, answer in results:
                if not succ:
                    answer.raiseException()

    @defer.inlineCallbacks
    def test_storm(self):
        numkeys = 10
        numcols = 10
        tries = 500

        with self.cluster_and_pool():
            yield self.make_standard_cfs()
            yield self.insert_dumb_rows(numkeys=numkeys, numcols=numcols)

            dlist = []
            for i in xrange(tries):
                keynum = '%03d' % random.randint(0, numkeys-1)
                key = 'key' + keynum
                col = '%s-%s-%03d' % (self.ksname, keynum, random.randint(0, numcols-1))
                d = self.pool.get(key, 'Standard1', col)
                d.addCallback(lambda c: c.column.value)
                d.addCallback(self.assertEqual, 'val-' + col)
                dlist.append(d)
            results = yield defer.DeferredList(dlist, consumeErrors=True)
            for succ, answer in results:
                if not succ:
                    answer.raiseException()

    @defer.inlineCallbacks
    def test_retrying(self):
        with self.cluster_and_pool():
            yield self.make_standard_cfs()
            yield self.insert_dumb_rows()

            d = self.pool.get('key000', 'Standard1/wait=1.0', '%s-000-000' % self.ksname,
                              retries=3)

            # give the timed 'get' a chance to start
            yield deferwait(0.2)

            # kill the connection handling the query
            conns = self.cluster.get_working_connections()
            self.assertEqual(len(conns), 1)
            node, proto = conns[0]
            proto.transport.loseConnection()

            # allow reconnect
            yield deferwait(0.2)
            newconns = self.cluster.get_working_connections()
            self.assertEqual(len(newconns), 1)
            newnode, newproto = newconns[0]
            # we want the preference to be reconnecting the same node
            self.assertEqual(node, newnode)
            answer = (yield d).column.value
            self.assertEqual(answer, 'val-%s-000-000' % self.ksname)

    def test_resubmit_to_new_conn(self):
        with self.cluster_and_pool():
            yield self.make_standard_cfs()
            yield self.insert_dumb_rows()

            d = self.pool.get('key005', 'Standard1/wait=1.0', '%s-005-000' % self.ksname,
                              retries=3)

            # give the timed 'get' a chance to start
            yield deferwait(0.2)

            # kill the backend handling the query
            conns = self.cluster.get_working_connections()
            self.assertEqual(len(conns), 1)
            node, proto = conns[0]
            node.stopService()

            # allow reconnect
            yield deferwait(0.2)
            newconns = self.cluster.get_working_connections()
            self.assertEqual(len(newconns), 1)
            newnode, newproto = newconns[0]
            # we want the preference to be reconnecting the same node
            self.assertNotEqual(node, newnode)
            answer = (yield d).column.value
            self.assertEqual(answer, 'val-%s-005-000' % self.ksname)

            node.startService()

    @defer.inlineCallbacks
    def test_adjust_pool_size(self):
        pool_size = 8

        with self.cluster_and_pool(pool_size=1):
            yield self.make_standard_cfs()
            yield self.insert_dumb_rows()

            # wait for initial conn
            yield deferwait(0.1)

            # turn up pool size once other nodes are known
            self.pool.adjustPoolSize(pool_size)
            yield deferwait(0.2)

            conns = self.cluster.get_connections()
            self.assertEqual(len(conns), pool_size)
            # one conn to each node
            self.assertEqual(len(set(n for (n,p) in conns)), pool_size)

            dlist = []
            for x in range(pool_size):
                d = self.pool.get('key001', 'Standard1/wait=0.5',
                                  '%s-001-002' % self.ksname, retries=3)
                d.addCallback(lambda c: c.column.value)
                d.addCallback(self.assertEqual, 'val-%s-001-002' % self.ksname)
                dlist.append(d)

            conns = self.cluster.get_connections()
            self.assertEqual(len(conns), pool_size)
            # one conn to each node
            self.assertEqual(len(set(n for (n,p) in conns)), pool_size)

            # turn down pool size
            self.pool.adjustPoolSize(pool_size - 2)
            yield deferwait(0.1)

            conns = self.cluster.get_connections()
            self.assertEqual(len(conns), pool_size - 2)

            result = yield defer.DeferredList(dlist, consumeErrors=True)
            for succ, answer in result:
                if not succ:
                    answer.raiseException()

    def test_zero_retries(self):
        pass

    def test_exhaust_retries(self):
        pass

    def test_connection_leveling(self):
        pass

    def test_huge_pool(self):
        pass

    def test_finish_and_die(self):
        pass

    def test_problematic_conns(self):
        pass

    def test_manual_node_add(self):
        pass

    def test_manual_node_remove(self):
        pass

    def test_conn_loss_during_idle(self):
        pass

    def test_conn_loss_during_request(self):
        pass

    def test_last_conn_loss_during_idle(self):
        pass

    @defer.inlineCallbacks
    def test_last_conn_loss_during_request(self):
        with self.cluster_and_pool(pool_size=1, num_nodes=1):
            yield self.make_standard_cfs()
            yield self.insert_dumb_rows()

            conns = self.cluster.get_connections()
            self.assertEqual(len(conns), 1)

            d = self.pool.get('key004', 'Standard1/wait=1.0',
                              '%s-004-008' % self.ksname, retries=4)
            yield deferwait(0.1)

            def cancel_if_no_conns(numconns, pending):
                numworkers = self.pool.num_working_conns()
                if numworkers == 0 and not d.called:
                    d.cancel()
            self.pool.on_insufficient_conns = cancel_if_no_conns

            workers = self.cluster.get_working_connections()
            self.assertEqual(len(workers), 1)
            node, proto = workers[0]
            node.stopService()
            yield deferwait(0.05)

            self.assertFired(d)
            yield self.assertFailure(d, defer.CancelledError)

        self.flushLoggedErrors()

class EnhancedCassanovaInterface(cassanova.CassanovaInterface):
    """
    Add a way to request operations which are guaranteed to take (at least) a
    given amount of time, for easier testing of things which might take a long
    time in the real world
    """

    def get(self, key, column_path, consistency_level):
        args = []
        if '/' in column_path.column_family:
            parts = column_path.column_family.split('/')
            column_path.column_family = parts[0]
            args = parts[1:]
        d = defer.maybeDeferred(cassanova.CassanovaInterface.get, self, key,
                                column_path, consistency_level)
        for arg in args:
            if arg.startswith('wait='):
                waittime = float(arg[5:])
                def doWait(x):
                    waiter = deferwait(waittime, x)
                    self.service.waiters.append(waiter)
                    return waiter
                d.addCallback(doWait)
        return d

class EnhancedCassanovaFactory(cassanova.CassanovaFactory):
    handler_factory = EnhancedCassanovaInterface

class EnhancedCassanovaNode(cassanova.CassanovaNode):
    factory = EnhancedCassanovaFactory

    def endpoint_str(self):
        return '%s:%d' % (self.addr.host, self.addr.port)

class FakeCassandraCluster(cassanova.CassanovaService):
    """
    Tweak the standard Cassanova service to allow nodes to run on the same
    interface, but different ports. CassandraClusterPool already knows how
    to understand the 'host:port' type of endpoint description in
    describe_ring output.
    """

    def __init__(self, num_nodes, start_port=41356, interface='127.0.0.1'):
        cassanova.CassanovaService.__init__(self, start_port)
        self.waiters = []
        self.iface = interface
        for n in range(num_nodes):
            self.add_node_on_port(start_port + n)
        # make a non-system keyspace so that describe_ring can work
        self.keyspaces['dummy'] = cassanova.KsDef(
            'dummy',
            replication_factor=1,
            strategy_class='org.apache.cassandra.locator.SimpleStrategy',
            cf_defs=[]
        )

    def add_node_on_port(self, port, token=None):
        node = EnhancedCassanovaNode(port, self.iface, token=token)
        node.setServiceParent(self)
        self.ring[node.mytoken] = node

    def stopService(self):
        cassanova.CassanovaService.stopService(self)
        for d in self.waiters:
            if not d.called:
                d.cancel()
