import json
import logging
import os
from functools import partial
from typing import Dict, Any
import itertools


import pytest
from ledger.compact_merkle_tree import CompactMerkleTree
from ledger.ledger import Ledger

from plenum.common.looper import Looper
from plenum.common.raet import initLocalKeep
from plenum.common.txn import TXN_TYPE, DATA, NEW_NODE, ALIAS, CLIENT_PORT, \
    CLIENT_IP
from plenum.common.types import HA, CLIENT_STACK_SUFFIX
from plenum.common.util import getNoInstances, TestingHandler, getConfig
from plenum.test.eventually import eventually, eventuallyAll
from plenum.test.helper import TestNodeSet, genNodeReg, Pool, \
    ensureElectionsDone, checkNodesConnected, genTestClient, randomOperation, \
    checkReqAck, checkLastClientReqForNode, getPrimaryReplica, \
    checkRequestReturnedToNode, \
    checkSufficientRepliesRecvd, checkViewNoForNodes, TestNode
from plenum.test.node_request.node_request_helper import checkPrePrepared, \
    checkPropagated, checkPrepared, checkCommited


def getValueFromModule(request, name: str, default: Any = None):
    """
    Gets an attribute from the request's module if attribute is found
    else return the default value

    :param request:
    :param name: name of attribute to get from module
    :param default: value to return if attribute was not found
    :return: value of the attribute if attribute was found in module else the default value
    """
    if hasattr(request.module, name):
        value = getattr(request.module, name)
        logging.info("found {} in the module: {}".
                     format(name, value))
    else:
        value = default if default is not None else None
        logging.info("no {} found in the module, using the default: {}".
                     format(name, value))
    return value


@pytest.fixture(scope="module")
def keySharedNodes(startedNodes):
    for n in startedNodes:
        n.startKeySharing()
    return startedNodes


@pytest.fixture(scope="module")
def startedNodes(nodeSet, looper):
    for n in nodeSet:
        n.start(looper.loop)
    return nodeSet


@pytest.fixture(scope="module")
def whitelist(request):
    return getValueFromModule(request, "whitelist", [])


@pytest.fixture(scope="function", autouse=True)
def logcapture(request, whitelist):
    whiteListedExceptions = ['seconds to run once nicely',
                             'Executing %s took %.3f seconds',
                             'is already stopped',
                             'Error while running coroutine'] + whitelist

    def tester(record):
        isBenign = record.levelno not in [logging.ERROR, logging.CRITICAL]
        # TODO is this sufficient to test if a log is from test or not?
        isTest = os.path.sep + 'test' in record.pathname
        isWhiteListed = bool([w for w in whiteListedExceptions
                              if w in record.msg])
        assert isBenign or isTest or isWhiteListed

    ch = TestingHandler(tester)
    logging.getLogger().addHandler(ch)

    request.addfinalizer(lambda: logging.getLogger().removeHandler(ch))
    return whiteListedExceptions


@pytest.yield_fixture(scope="module")
def nodeSet(request, tdir, nodeReg):
    primaryDecider = getValueFromModule(request, "PrimaryDecider", None)
    with TestNodeSet(nodeReg=nodeReg, tmpdir=tdir,
                     primaryDecider=primaryDecider) as ns:
        yield ns


@pytest.fixture(scope="session")
def counter():
    return itertools.count()


@pytest.fixture(scope='module')
def tdir(tmpdir_factory, counter):
    tempdir = os.path.join(tmpdir_factory.getbasetemp().strpath,
                           str(next(counter)))
    logging.debug("module-level temporary directory: {}".format(tempdir))
    return tempdir


@pytest.fixture(scope='function')
def tdir_for_func(tmpdir_factory, counter):
    tempdir = os.path.join(tmpdir_factory.getbasetemp().strpath,
                           str(next(counter)))
    logging.debug("function-level temporary directory: {}".format(tempdir))
    return tempdir


@pytest.fixture(scope="module")
def nodeReg(request) -> Dict[str, HA]:
    nodeCount = getValueFromModule(request, "nodeCount", 4)
    return genNodeReg(count=nodeCount)


@pytest.yield_fixture(scope="module")
def unstartedLooper(nodeSet):
    with Looper(nodeSet, autoStart=False) as l:
        yield l


@pytest.fixture(scope="module")
def looper(unstartedLooper):
    unstartedLooper.autoStart = True
    unstartedLooper.startall()
    return unstartedLooper


@pytest.fixture(scope="module")
def pool(tmpdir_factory, counter):
    return Pool(tmpdir_factory, counter)


@pytest.fixture(scope="module")
def ready(looper, keySharedNodes):
    looper.run(checkNodesConnected(keySharedNodes))
    return keySharedNodes


@pytest.fixture(scope="module")
def up(looper, ready):
    ensureElectionsDone(looper=looper, nodes=ready, retryWait=1, timeout=30)


# noinspection PyIncorrectDocstring
@pytest.fixture(scope="module")
def ensureView(nodeSet, looper, up):
    """
    Ensure that all the nodes in the nodeSet are in the same view.
    """
    return looper.run(eventually(checkViewNoForNodes, nodeSet, timeout=3))


@pytest.fixture("module")
def delayedPerf(nodeSet):
    for node in nodeSet:
        node.delayCheckPerformance(20)


@pytest.fixture(scope="module")
def client1(looper, nodeSet, tdir, up):
    client = genTestClient(nodeSet, tmpdir=tdir)
    looper.add(client)
    looper.run(client.ensureConnectedToNodes())
    return client


@pytest.fixture(scope="module")
def request1():
    return randomOperation()


@pytest.fixture(scope="module")
def sent1(client1, request1):
    return client1.submit(request1)[0]


@pytest.fixture(scope="module")
def reqAcked1(looper, nodeSet, client1, sent1, faultyNodes):
    coros = [partial(checkLastClientReqForNode, node, sent1)
             for node in nodeSet]
    looper.run(eventuallyAll(*coros,
                             totalTimeout=10,
                             acceptableFails=faultyNodes))

    coros2 = [partial(checkReqAck, client1, node, sent1.reqId)
              for node in nodeSet]
    looper.run(eventuallyAll(*coros2,
                             totalTimeout=5,
                             acceptableFails=faultyNodes))

    return sent1


@pytest.fixture(scope="module")
def faultyNodes(request):
    return getValueFromModule(request, "faultyNodes", 0)


@pytest.fixture(scope="module")
def propagated1(looper,
                nodeSet,
                up,
                reqAcked1,
                faultyNodes):
    checkPropagated(looper, nodeSet, reqAcked1, faultyNodes)
    return reqAcked1


@pytest.fixture(scope="module")
def preprepared1(looper, nodeSet, propagated1, faultyNodes):
    checkPrePrepared(looper,
                     nodeSet,
                     propagated1,
                     range(getNoInstances(len(nodeSet))),
                     faultyNodes)
    return propagated1


@pytest.fixture(scope="module")
def prepared1(looper, nodeSet, client1, preprepared1, faultyNodes):
    checkPrepared(looper,
                  nodeSet,
                  preprepared1,
                  range(getNoInstances(len(nodeSet))),
                  faultyNodes)
    return preprepared1


@pytest.fixture(scope="module")
def committed1(looper, nodeSet, client1, prepared1, faultyNodes):
    checkCommited(looper,
                  nodeSet,
                  prepared1,
                  range(getNoInstances(len(nodeSet))),
                  faultyNodes)
    return prepared1


@pytest.fixture(scope="module")
def replied1(looper, nodeSet, client1, committed1):
    for instId in range(getNoInstances(len(nodeSet))):
        getPrimaryReplica(nodeSet, instId)

        looper.run(*[eventually(checkRequestReturnedToNode,
                                node,
                                client1.defaultIdentifier,
                                committed1.reqId,
                                committed1.digest,
                                instId,
                                retryWait=1, timeout=30)
                     for node in nodeSet])

        looper.run(eventually(
                checkSufficientRepliesRecvd,
                client1.inBox,
                committed1.reqId,
                2,
                retryWait=2,
                timeout=30))
    return committed1


@pytest.yield_fixture(scope="module")
def looperWithoutNodeSet():
    with Looper(debug=True) as looper:
        yield looper


@pytest.fixture(scope="module")
def poolTxnNodeNames():
    return "Alpha", "Beta", "Gamma", "Delta"


@pytest.fixture(scope="module")
def poolTxnClientNames():
    return "Alice",


@pytest.fixture(scope="module")
def poolTxnStewardNames():
    return "Bob",


@pytest.fixture(scope="module")
def conf():
    return getConfig()


@pytest.fixture(scope="module")
def tconf(conf, tdir):
    conf.baseDir = tdir
    return conf


@pytest.fixture(scope="module")
def dirName():
    return os.path.dirname


@pytest.fixture(scope="module")
def poolTxnData(dirName):
    filePath = os.path.join(dirName(__file__), "node_and_client_info.json")
    return json.loads(open(filePath).read().strip())


@pytest.fixture(scope="module")
def tdirWithPoolTxns(poolTxnData, tdir, tconf):
    ledger = Ledger(CompactMerkleTree(),
           dataDir=tdir,
           fileName=tconf.poolTransactionsFile)
    for item in poolTxnData["txns"]:
        ledger.add(item)
    return tdir


@pytest.fixture(scope="module")
def tdirWithNodeKeepInited(tdir, poolTxnData, poolTxnNodeNames):
    seeds = poolTxnData["seeds"]
    for nName in poolTxnNodeNames:
        initLocalKeep(nName, tdir, *seeds[nName], override=True)


@pytest.fixture(scope="module")
def poolTxnClientData(poolTxnClientNames, poolTxnData):
    name = poolTxnClientNames[0]
    seeds = poolTxnData["seeds"][name]
    return (name, ) + tuple(s.encode() for s in seeds)


@pytest.fixture(scope="module")
def poolTxnStewardData(poolTxnStewardNames, poolTxnData):
    name = poolTxnStewardNames[0]
    seeds = poolTxnData["seeds"][name]
    return (name, ) + tuple(s.encode() for s in seeds)


@pytest.yield_fixture(scope="module")
def txnPoolNodeSet(tdirWithPoolTxns, tconf, poolTxnNodeNames,
                   tdirWithNodeKeepInited):
    with Looper(debug=True) as looper:
        nodes = []
        for nm in poolTxnNodeNames:
            node = TestNode(nm, basedirpath=tdirWithPoolTxns, config=tconf)
            looper.add(node)
            nodes.append(node)

        looper.run(eventually(checkNodesConnected, nodes, retryWait=1, timeout=5))
        yield nodes


@pytest.fixture(scope="module")
def txnPoolCliNodeReg(poolTxnData):
    cliNodeReg = {}
    for txn in poolTxnData["txns"]:
        if txn[TXN_TYPE] == NEW_NODE:
            data = txn[DATA]
            cliNodeReg[data[ALIAS]+CLIENT_STACK_SUFFIX] = HA(data[CLIENT_IP],
                                                             data[CLIENT_PORT])
    return cliNodeReg
