##  Airhook Protocol http://airhook.org/protocol.html
##  Copyright 2002, Andrew Loewenstern, All Rights Reserved

from random import uniform as rand
from struct import pack, unpack
from time import time
import unittest
from bisect import insort_left

from twisted.internet import protocol
from twisted.internet import abstract
from twisted.internet import reactor
from twisted.internet import app
from twisted.internet import interfaces

# flags
FLAG_AIRHOOK = 128
FLAG_OBSERVED = 16
FLAG_SESSION = 8
FLAG_MISSED = 4
FLAG_NEXT = 2
FLAG_INTERVAL = 1

MAX_PACKET_SIZE = 1450

pending = 0
sent = 1
confirmed = 2



class Airhook(protocol.DatagramProtocol):       
    def __init__(self):
        self.noisy = None
        # this should be changed to storage that drops old entries
        self.connections = {}
        
    def datagramReceived(self, datagram, addr):
        #print `addr`, `datagram`
        #if addr != self.addr:
        self.connectionForAddr(addr).datagramReceived(datagram)

    def connectionForAddr(self, addr):
        if not self.connections.has_key(addr):
            conn = self.connection()
            conn.protocol = self.factory.buildProtocol(addr)
            conn.protocol.makeConnection(conn)
            conn.makeConnection(self.transport)
            conn.addr = addr
            self.connections[addr] = conn
        else:
            conn = self.connections[addr]
        return conn
#    def makeConnection(self, transport):
#        protocol.DatagramProtocol.makeConnection(self, transport)
#        tup = transport.getHost()
#        self.addr = (tup[1], tup[2])
        
class AirhookPacket:
    def __init__(self, msg):
        self.datagram = msg
        self.oseq =  ord(msg[1])
        self.seq = unpack("!H", msg[2:4])[0]
        self.flags = ord(msg[0])
        self.session = None
        self.observed = None
        self.next = None
        self.missed = []
        self.msgs = []
        skip = 4
        if self.flags & FLAG_OBSERVED:
            self.observed = unpack("!L", msg[skip:skip+4])[0]
            skip += 4
        if self.flags & FLAG_SESSION:
            self.session =  unpack("!L", msg[skip:skip+4])[0]
            skip += 4
        if self.flags & FLAG_NEXT:
            self.next =  ord(msg[skip])
            skip += 1
        if self.flags & FLAG_MISSED:
            num = ord(msg[skip]) + 1
            skip += 1
            for i in range(num):
                self.missed.append( ord(msg[skip+i]))
            skip += num
        if self.flags & FLAG_NEXT:
            while len(msg) - skip > 0:
                n = ord(msg[skip]) + 1
                skip+=1
                self.msgs.append( msg[skip:skip+n])
                skip += n

class AirhookConnection(protocol.ConnectedDatagramProtocol, interfaces.IUDPConnectedTransport):
    def __init__(self):        
        self.outSeq = 0  # highest sequence we have sent, can't be 255 more than obSeq
        self.obSeq = 0   # highest sequence confirmed by remote
        self.inSeq = 0   # last received sequence
        self.observed = None  # their session id
        self.sessionID = long(rand(0, 2**32))  # our session id
        
        self.lastTransmit = 0  # time we last sent a packet with messages
        self.lastReceieved = 0 # time we last received a packet with messages
        self.lastTransmitSeq = -1 # last sequence we sent a packet
        self.state = pending
        
        self.outMsgs = [None] * 256  # outgoing messages  (seq sent, message), index = message number
        self.omsgq = [] # list of messages to go out
        self.imsgq = [] # list of messages coming in
        self.sendSession = None  # send session/observed fields until obSeq > sendSession
        self.response = 0 # if we know we have a response now (like resending missed packets)
        self.noisy = 0
        self.scheduled = 0 # a sendNext is scheduled, don't schedule another
        self.resetMessages()
    
    def resetMessages(self):
        self.weMissed = []
        self.inMsg = 0   # next incoming message number
        self.outMsgNums = [None] * 256 # outgoing message numbers i = outNum % 256
        self.next = 0  # next outgoing message number

    def datagramReceived(self, datagram):
        if not datagram:
            return
        p = AirhookPacket(datagram)
        
        # check to make sure sequence number isn't out of order
        if (p.seq - self.inSeq) % 2**16 >= 256:
            return
            
        # check for state change
        if self.state == pending:
            if p.observed != None and p.session != None:
                if p.observed == self.sessionID:
                    self.observed = p.session
                    self.state = confirmed
                else:
                    # bogus packet!
                    return
            elif p.session != None:
                self.observed = p.session
                self.state = sent
                self.response = 1
        elif self.state == sent:
            if p.observed != None and p.session != None:
                if p.observed == self.sessionID:
                    self.observed = p.session
                    self.sendSession = self.outSeq
                    self.state = confirmed
            if p.session != None:
                if not self.observed:
                    self.observed = p.session
                elif self.observed != p.session:
                    self.state = pending
                    self.resetMessages()
                    self.inSeq = p.seq
        elif self.state == confirmed:
            if p.session != None or p.observed != None :
                if (p.session != None and p.session != self.observed) or (p.observed != None and p.observed != self.sessionID):
                    self.state = pending
                    self.resetMessages()
                    self.inSeq = p.seq
    
        if self.state != pending:	
            msgs = []		
            missed = []
            
            # see if they need us to resend anything
            for i in p.missed:
                if self.outMsgs[i] != None:
                    self.omsgq.append(self.outMsgs[i])
                    self.outMsgs[i] = None
                    
            # see if we missed any messages
            if p.next != None:
                missed_count = (p.next - self.inMsg) % 256
                if missed_count:
                    self.lastReceived = time()
                    for i in range(missed_count):
                        missed += [(self.outSeq, (self.inMsg + i) % 256)]
                    self.weMissed += missed
                    self.response = 1
                # record highest message number seen
                self.inMsg = (p.next + len(p.msgs)) % 256
            
            # append messages, update sequence
            self.imsgq += p.msgs
            
        if self.state == confirmed:
            # unpack the observed sequence
            tseq = unpack('!H', pack('!H', self.outSeq)[0] +  chr(p.oseq))[0]
            if ((self.outSeq - tseq)) % 2**16 > 255:
                tseq = unpack('!H', chr(ord(pack('!H', self.outSeq)[0]) - 1) + chr(p.oseq))[0]
            self.obSeq = tseq

        self.inSeq = p.seq

        self.lastReceived = time()
        self.dataCameIn()
        
        self.schedule()
        
    def sendNext(self):
        flags = 0
        header = chr(self.inSeq & 255) + pack("!H", self.outSeq)
        ids = ""
        missed = ""
        msgs = ""
        
        # session / observed logic
        if self.state == pending:
            flags = flags | FLAG_SESSION
            ids +=  pack("!L", self.sessionID)
            self.state = sent
        elif self.state == sent:
            if self.observed != None:
                flags = flags | FLAG_SESSION | FLAG_OBSERVED
                ids +=  pack("!LL", self.observed, self.sessionID)
            else:
                flags = flags | FLAG_SESSION
                ids +=  pack("!L", self.sessionID)

        else:
            if self.state == sent or self.sendSession:
                if self.state == confirmed and (self.obSeq - self.sendSession) % 2**16 < 256:
                    self.sendSession = None
                else:
                    flags = flags | FLAG_SESSION | FLAG_OBSERVED
                    ids +=  pack("!LL", self.observed, self.sessionID)
        
        # missed header
        if self.obSeq >= 0:
            self.weMissed = filter(lambda a: a[0] > self.obSeq, self.weMissed)

        if self.weMissed:
            flags = flags | FLAG_MISSED
            missed += chr(len(self.weMissed) - 1)
            for i in self.weMissed:
                missed += chr(i[1])
                
        # append any outgoing messages
        if self.state == confirmed and self.omsgq:
            first = self.next
            outstanding = (256 + (((self.next - 1) % 256) - self.outMsgNums[self.obSeq % 256])) % 256
            while len(self.omsgq) and outstanding  < 255 and len(self.omsgq[-1]) + len(msgs) + len(missed) + len(ids) + len(header) + 1 <= MAX_PACKET_SIZE:
                msg = self.omsgq.pop()
                msgs += chr(len(msg) - 1) + msg
                self.outMsgs[self.next] = msg
                self.next = (self.next + 1) % 256
                outstanding+=1
        # update outgoing message stat
        if msgs:
            flags = flags | FLAG_NEXT
            ids += chr(first)
            self.lastTransmitSeq = self.outSeq
            #self.outMsgNums[self.outSeq % 256] = first
        #else:
        self.outMsgNums[self.outSeq % 256] = (self.next - 1) % 256
        
        # do we need a NEXT flag despite not having sent any messages?
        if not flags & FLAG_NEXT and (256 + (((self.next - 1) % 256) - self.outMsgNums[self.obSeq % 256])) % 256 > 0:
            flags = flags | FLAG_NEXT
            ids += chr(self.next)
        
        # update stats and send packet
        packet = chr(flags) + header + ids + missed + msgs
        self.outSeq = (self.outSeq + 1) % 2**16
        self.lastTransmit = time()
        self.transport.write(packet, self.addr)
        
        self.scheduled = 0
        self.schedule()
        
    def timeToSend(self):
        # any outstanding messages and are we not too far ahead of our counterparty?
        if len(self.omsgq) > 0 and self.state != sent and (self.next + 1) % 256 != self.outMsgNums[self.obSeq % 256] and (self.outSeq - self.obSeq) % 2**16 < 256:
            return (1, 0)
        # do we explicitly need to send a response?
        elif self.response:
            self.response = 0
            return (1, 0)
        # have we not sent anything in a while?
        elif time() - self.lastTransmit > 1.0:
            return (1, 1)
        elif self.state == pending:
            return (1, 1)
            
        # nothing to send
        return (0, 0)

    def schedule(self):
        tts, t = self.timeToSend()
        if tts and not self.scheduled:
            self.scheduled = 1
            reactor.callLater(t, self.sendNext)
        
    def write(self, data):
        # micropackets can only be 255 bytes or less
        if len(data) <= 255:
            self.omsgq.insert(0, data)
        self.schedule()
        
    def dataCameIn(self):
        """
        called when we get a packet bearing messages
        """
        for msg in self.imsgq:
            self.protocol.dataReceived(msg)
        self.imsgq = []

class ustr(str):
    """
        this subclass of string encapsulates each ordered message, caches it's sequence number,
        and has comparison functions to sort by sequence number
    """
    def getseq(self):
        if not hasattr(self, 'seq'):
            self.seq = unpack("!H", self[0:2])[0]
        return self.seq
    def __lt__(self, other):
        return (self.getseq() - other.getseq()) % 2**16 > 255
    def __le__(self, other):
        return (self.getseq() - other.getseq()) % 2**16 > 255 or self.__eq__(other)
    def __eq__(self, other):
        return self.getseq() == other.getseq()
    def __ne__(self, other):
        return self.getseq() != other.getseq()
    def __gt__(self, other):
        return (self.getseq() - other.getseq()) % 2**16 < 256  and not self.__eq__(other)
    def __ge__(self, other):
        return (self.getseq() - other.getseq()) % 2**16 < 256
        
class StreamConnection(AirhookConnection):
    """
        this implements a simple protocol for a stream over airhook
        this is done for convenience, instead of making it a twisted.internet.protocol....
        the first two octets of each message are interpreted as a 16-bit sequence number
        253 bytes are used for payload
        
    """
    def __init__(self):
        AirhookConnection.__init__(self)
        self.oseq = 0
        self.iseq = 0
        self.q = []

    def dataCameIn(self):
        # put 'em together
        for msg in self.imsgq:
            insort_left(self.q, ustr(msg))
        self.imsgq = []
        data = ""
        while self.q and self.iseq == self.q[0].getseq():
            data += self.q[0][2:]
            self.q = self.q[1:]
            self.iseq = (self.iseq + 1) % 2**16
        if data != '':
            self.protocol.dataReceived(data)
        
    def write(self, data):
        # chop it up and queue it up
        while data:
            p = pack("!H", self.oseq) + data[:253]
            self.omsgq.insert(0, p)
            data = data[253:]
            self.oseq = (self.oseq + 1) % 2**16

        self.schedule()
        
    def writeSequence(self, sequence):
        for data in sequence:
            self.write(data)


def listenAirhook(port, factory):
    ah = Airhook()
    ah.connection = AirhookConnection
    ah.factory = factory
    reactor.listenUDP(port, ah)
    return ah

def listenAirhookStream(port, factory):
    ah = Airhook()
    ah.connection = StreamConnection
    ah.factory = factory
    reactor.listenUDP(port, ah)
    return ah

    