from src.util.hash import std_hash
from secrets import randbits
from random import randrange, choice
from src.types.peer_info import PeerInfo

import time
import asyncio
import math

TRIED_BUCKETS_PER_GROUP = 8
NEW_BUCKETS_PER_SOURCE_GROUP = 64
TRIED_BUCKET_COUNT = 256
NEW_BUCKET_COUNT = 1024
BUCKET_SIZE = 64
TRIED_COLLISION_SIZE = 10
NEW_BUCKETS_PER_ADDRESS = 8
LOG_TRIED_BUCKET_COUNT = 3
LOG_NEW_BUCKET_COUNT = 10
LOG_BUCKET_SIZE = 6
HORIZON_DAYS = 30
MAX_RETRIES = 3
MIN_FAIL_DAYS = 7
MAX_FAILURES = 10

# This is a Python port from 'CAddrInfo' class from Bitcoin core code.
class ExtendedPeerInfo:
    def __init__(
        self,
        peer_info,
        src_peer,
    ):
        self.peer_info = peer_info
        self.src = src_peer
        if src_peer is None:
            self.src = peer_info
        self.random_pos = None
        self.is_tried = False
        self.ref_count = 0
        self.nLastSuccess = 0
        self.nLastTry = 0
        self.nTime = peer_info.timestamp
        self.nAttempts = 0
        self.nLastCountAttempt = 0

    def to_string(self):
        return self.peer_info.host
        + " " + int(self.peer_info.port) 
        + " " self.src.host 
        + " " + int(self.src.port)

    @classmethod
    def from_string(cls, peer_str):
        blobs = peer_str.split(" ")
        assert len(blobs) == 4
        peer_info = PeerInfo(blobs[0], int(blobs[1]))
        src_peer = PeerInfo(blobs[2], int(blobs[3])) 
        return cls(peer_info, src_peer)

    def get_tried_bucket(self, nKey):
        hash1 = int.from_bytes(
            bytes(
                std_hash(
                    nKey.to_bytes(32, byteorder='big') 
                    + self.peer_info.get_key()
                )[:8]
            ), byteorder='big'
        )
        hash1 = hash1 % TRIED_BUCKETS_PER_GROUP
        hash2 = int.from_bytes(
            bytes(
                std_hash(
                    nKey.to_bytes(32, byteorder='big')
                    + self.peer_info.get_group()
                    + bytes([hash1])
                )[:8]
            ), byteorder='big'
        )
        return hash2 % TRIED_BUCKET_COUNT

    def get_new_bucket(self, nKey, src_peer=None):
        if src_peer is None:
            src_peer = self.src
        hash1 = int.from_bytes(
            bytes(
                std_hash(
                    nKey.to_bytes(32, byteorder='big')
                    + self.peer_info.get_group()
                    + src_peer.get_group()
                )[:8]
            ), byteorder='big'
        )
        hash1 = hash1 % NEW_BUCKETS_PER_SOURCE_GROUP
        hash2 = int.from_bytes(
            bytes(
                std_hash(
                    nKey.to_bytes(32, byteorder='big')
                    + src_peer.get_group()
                    + bytes([hash1])
                )[:8]
            ), byteorder='big'
        )
        return hash2 % NEW_BUCKET_COUNT

    def get_bucket_position(self, nKey, fNew, nBucket):
        ch = 'N' if fNew else 'K'
        hash1 = int.from_bytes(
            bytes(
                std_hash(
                    nKey.to_bytes(32, byteorder='big')
                    + ch.encode()
                    + nBucket.to_bytes(3, byteorder='big')
                    + self.peer_info.get_key()
                )[:8]
            ), byteorder='big'
        )
        return hash1 % BUCKET_SIZE

    def is_terrible(self, nNow=time.time()):
        # never remove things tried in the last minute
        if (self.nLastTry > 0 and self.nLastTry >= nNow - 60): 
            return False

        # came in a flying DeLorean
        if self.nTime > nNow + 10 * 60:
            return True

        # not seen in recent history
        if (
            self.nTime == 0
            or nNow - self.nTime > HORIZON_DAYS * 24 * 60 * 60
        ):
            return True

        # tried N times and never a success
        if (
            self.nLastSuccess == 0
            and self.nAttempts >= MAX_RETRIES
        ):
            return True

        # N successive failures in the last week
        if (
            nNow - self.nLastSuccess > MIN_FAIL_DAYS * 24 * 60 * 60 
            and self.nAttempts >= MAX_FAILURES
        ):
            return True

        return False

    def get_selection_chance(self, nNow=time.time()):
        fChance = 1.0
        nSinceLastTry = max(nNow - self.nLastTry, 0)
        # deprioritize very recent attempts away
        if nSinceLastTry < 60 * 10:
            fChance *= 0.01

        # deprioritize 66% after each failed attempt,
        # but at most 1/28th to avoid the search taking forever or overly penalizing outages.
        fChance *= pow(0.66, min(self.nAttempts, 8))
        return fChance


# This is a Python port from 'CAddrMan' class from Bitcoin core code.
class AddressManager:
    def __init__(self):
        self.id_count = 0
        self.nKey = randbits(256)
        self.random_pos = []
        self.tried_matrix = [
            [
                -1 for x in range(BUCKET_SIZE)
            ]
            for y in range(TRIED_BUCKET_COUNT)
        ]
        self.new_matrix = [
            [
                -1 for x in range(BUCKET_SIZE)
            ]
            for y in range(NEW_BUCKET_COUNT)
        ]
        self.tried_count = 0
        self.new_count = 0
        self.map_addr = {}
        self.map_info = {}
        self.nLastGood = 1
        self.tried_collisions = []
        self.lock = asyncio.Lock()

    def create_(self, addr: PeerInfo, addr_src: PeerInfo):
        self.id_count += 1
        node_id = self.id_count
        self.map_info[node_id] = ExtendedPeerInfo(addr, addr_src)
        self.map_addr[addr.host] = node_id
        self.map_info[node_id].random_pos = len(self.random_pos)
        self.random_pos.append(node_id)
        return (self.map_info[node_id], node_id)

    def find_(self, addr: PeerInfo):
        if addr.host not in self.map_addr:
            return (None, None)
        node_id = self.map_addr[addr.host]
        if node_id not in self.map_info:
            return (None, node_id)
        return (self.map_info[node_id], node_id)

    def swap_random_(self, rand_pos_1, rand_pos_2):
        if rand_pos_1 == rand_pos_2:
            return
        assert(rand_pos_1 < len(self.random_pos) and rand_pos_2 < len(self.random_pos))
        node_id_1 = self.random_pos[rand_pos_1]
        node_id_2 = self.random_pos[rand_pos_2]
        self.map_info[node_id_1].random_pos = rand_pos_2
        self.map_info[node_id_2].random_pos = rand_pos_1
        self.random_pos[rand_pos_1] = node_id_2
        self.random_pos[rand_pos_2] = node_id_1

    def make_tried_(self, info, node_id):
        for bucket in range(NEW_BUCKET_COUNT):
            pos = info.get_bucket_position(self.nKey, True, bucket)
            if self.new_matrix[bucket][pos] == node_id:
                self.new_matrix[bucket][pos] = -1
                info.ref_count -= 1
        assert info.ref_count == 0
        self.new_count -= 1
        cur_bucket = info.get_tried_bucket(self.nKey)
        cur_bucket_pos = info.get_bucket_position(self.nKey, False, cur_bucket)
        if self.tried_matrix[cur_bucket][cur_bucket_pos] != -1:
            # Evict the old node from the tried table.
            node_id_evict = self.tried_matrix[cur_bucket][cur_bucket_pos]
            assert node_id_evict in self.map_info
            old_info = self.map_info[node_id_evict]
            old_info.is_tried = False
            self.tried_matrix[cur_bucket][cur_bucket_pos] = -1
            self.tried_count -= 1
            # Find its position into new table.
            new_bucket = old_info.get_new_bucket(self.nKey)
            new_bucket_pos = old_info.get_bucket_position(self.nKey, True, new_bucket)
            self.clear_new_(new_bucket, new_bucket_pos)
            old_info.ref_count = 1
            self.new_matrix[new_bucket][new_bucket_pos] = node_id_evict
            self.new_count += 1
        self.tried_matrix[cur_bucket][cur_bucket_pos] = node_id
        self.tried_count += 1
        info.is_tried = True

    def clear_new_(self, bucket, pos):
        if self.new_matrix[bucket][pos] != -1:
            delete_id = self.new_matrix[bucket][pos]
            delete_info = self.map_info[delete_id]
            assert delete_info.ref_count > 0
            delete_info.ref_count -= 1
            self.new_matrix[bucket][pos] = -1
            if delete_info.ref_count == 0:
                self.delete_new_entry_(delete_id)

    def mark_good_(self, addr, test_before_evict, nTime):
        self.nLastGood = nTime
        (info, node_id) = self.find_(addr)
        if info is None:
            return

        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        # update info
        info.nLastSuccess = nTime
        info.nLastTry = nTime
        info.nAttempts = 0
        # nTime is not updated here, to avoid leaking information about
        # currently-connected peers.

        # if it is already in the tried set, don't do anything else
        if info.is_tried:
            return

        # find a bucket it is in now
        nRnd = randrange(NEW_BUCKET_COUNT)
        nUBucket = -1
        for n in range(NEW_BUCKET_COUNT):
            nB = (n + nRnd) % NEW_BUCKET_COUNT
            nBpos = info.get_bucket_position(self.nKey, True, nB)
            if self.new_matrix[nB][nBpos] == node_id:
                nUBucket = nB
                break

        # if no bucket is found, something bad happened;
        if nUBucket == -1:
            return

        # NOTE(Florin): Double check this. It's not used anywhere else.

        # which tried bucket to move the entry to
        tried_bucket = info.get_tried_bucket(self.nKey)
        tried_bucket_pos = info.get_bucket_position(self.nKey, False, tried_bucket)

        # Will moving this address into tried evict another entry?
        if (test_before_evict and self.tried_matrix[tried_bucket][tried_bucket_pos] != -1):
            if len(self.tried_collisions) < TRIED_COLLISION_SIZE:
                if node_id not in self.tried_collisions:
                    self.tried_collisions.append(node_id)
        else:
            self.make_tried_(info, node_id)

    def delete_new_entry_(self, node_id):
        info = self.map_info[node_id]
        self.swap_random_(info.random_pos, len(self.random_pos) - 1)
        self.random_pos = self.random_pos[:-1]
        del self.map_addr[info.peer_info.host]
        del self.map_info[node_id]
        self.new_count -= 1

    def add_to_new_table_(self, addr, source, nTimePenalty):
        is_unique = False
        (info, node_id) = self.find_(addr)
        if (
            info is not None
            and info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            nTimePenalty = 0

        if info is not None:
            # periodically update timestamp
            currently_online = (time.time() - addr.nTime < 24 * 60 * 60)
            update_interval = 60 * 60 if currently_online else 24 * 60 * 60
            if (
                addr.timestamp > 0
                and (
                    info.nTime > 0
                    or info.nTime < addr.timestamp - update_interval - nTimePenalty
                )
            ):
                info.nTime = max(0, addr.timestamp - nTimePenalty)

            # do not update if no new information is present
            if (
                addr.timestamp == 0
                or (
                    info.nTime > 0
                    and addr.timestamp <= info.nTime
                )
            ):
                return False

            # do not update if the entry was already in the "tried" table
            if info.is_tried:
                return False

            # do not update if the max reference count is reached
            if info.ref_count == NEW_BUCKETS_PER_ADDRESS:
                return False

            # stochastic test: previous ref_count == N: 2^N times harder to increase it
            factor = (1 << info.ref_count)
            if (
                factor > 1
                and randrange(factor) != 0
            ):
                return False
        else:
            (info, node_id) = self.create_(addr, source)
            info.nTime = max(0, info.nTime - nTimePenalty)
            self.new_count += 1
            is_unique = True

        nUBucket = info.get_new_bucket(self.nKey, source)
        nUBucketPos = info.get_bucket_position(self.nKey, True, nUBucket)
        if self.new_matrix[nUBucket][nUBucketPos] != node_id:
            fInsert = (self.new_matrix[nUBucket][nUBucketPos] == -1)
            if not fInsert:
                info_existing = self.map_info[
                    self.new_matrix[nUBucket][nUBucketPos]
                ]
                if (info_existing.is_terrible() or (info_existing.ref_count > 1 and info.ref_count == 0)):
                    fInsert = True
            if fInsert:
                self.clear_new_(nUBucket, nUBucketPos)
                info.ref_count += 1
                self.new_matrix[nUBucket][nUBucketPos] = node_id
            else:
                if info.ref_count == 0:
                    self.delete_new_entry_(node_id)
        return is_unique

    def attempt_(self, addr, count_failures, nTime):
        info, _ = self.find_(addr)
        if info is None:
            return

        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        info.nLastTry = nTime
        if (count_failures and info.nLastCountAttempt < info.nLastGood):
            info.nLastCountAttempt = nTime
            info.nAttempts += 1

    def select_peer_(self, new_only):
        if len(self.random_pos) == 0:
            return None

        if (new_only and self.new_count == 0):
            return None

        # Use a 50% chance for choosing between tried and new table entries.
        if (
            not new_only
            and self.tried_count > 0
            and (
                self.new_count == 0
                or randrange(2) == 0
            )
        ):
            fChanceFactor = 1.0
            while True:
                nKBucket = randrange(TRIED_BUCKET_COUNT)
                nKBucketPos = randrange(BUCKET_SIZE)
                while self.tried_matrix[nKBucket][nKBucketPos] == -1:
                    nKBucket = (nKBucket + randbits(LOG_TRIED_BUCKET_COUNT)) % TRIED_BUCKET_COUNT
                    nKBucketPos = (nKBucketPos + randbits(LOG_BUCKET_SIZE)) % BUCKET_SIZE
                node_id = self.tried_matrix[nKBucket][nKBucketPos]
                info = self.map_info[node_id]
                if randbits(30) < (fChanceFactor * info.get_selection_chance() * (1 << 30)):
                    return info.peer_info
                fChanceFactor *= 1.2
        else:
            fChanceFactor = 1.0
            while True:
                nUBucket = randrange(NEW_BUCKET_COUNT)
                nUBucketPos = randrange(BUCKET_SIZE)
                while self.new_matrix[nUBucket][nUBucketPos] == -1:
                    nUBucket = (nUBucket + randbits(LOG_NEW_BUCKET_COUNT)) % NEW_BUCKET_COUNT
                    nUBucketPos = (nUBucketPos + randbits(LOG_BUCKET_SIZE)) % BUCKET_SIZE
                node_id = self.new_matrix[nUBucket][nUBucketPos]
                info = self.map_info[node_id]
                if (randbits(30) < fChanceFactor * info.get_selection_chance() * (1 << 30)):
                    return info.peer_info
                fChanceFactor *= 1.2

    def resolve_tried_collisions_(self):
        for node_id in self.tried_collisions[:]:
            resolved = False
            if node_id not in self.map_info:
                resolved = True
            else:
                info = self.map_info[node_id]
                peer = info.peer_info
                tried_bucket = info.get_tried_bucket(self.nKey)
                tried_bucket_pos = info.get_bucket_position(self.nKey, False, tried_bucket)
                if self.tried_matrix[tried_bucket][tried_bucket_pos] != -1:
                    old_id = self.tried_matrix[tried_bucket][tried_bucket_pos]
                    old_info = self.map_info[old_id]
                    if time.time() - old_info.nLastSuccess < 4 * 60 * 60:
                        resolved = True
                    elif time.time() - old_info.nLastTry < 4 * 60 * 60:
                        if time.time() - old_info.nLastTry > 60:
                            self.mark_good_(peer, False, time.time())
                            resolved = True
                    elif time.time() - info.nLastSuccess > 40 * 60:
                        self.mark_good_(peer, False, time.time())
                        resolved = True
                else:
                    self.mark_good_(peer, False, time.time())
                    resolved = True
            if resolved:
                self.tried_collisions.remove(node_id)

    def select_tried_collision_(self):
        if len(self.tried_collisions) == 0:
            return None
        new_id = choice(self.tried_collisions)
        if new_id not in self.map_info:
            self.tried_collisions.remove(new_id)
            return None
        new_info = self.map_info[new_id]
        tried_bucket = new_info.get_tried_bucket(self.nKey)
        tried_bucket_pos = new_info.get_bucket_position(self.nKey, False, tried_bucket)

        old_id = self.tried_matrix[tried_bucket][tried_bucket_pos]
        return self.map_info[old_id]

    def get_peers_(self):
        addr = []
        num_nodes = 23 * len(self.random_pos) // 100
        if num_nodes > 2500:
            num_nodes = 2500
        for n in range(len(self.random_pos)):
            if len(addr) >= num_nodes:
                return addr

            nRndPos = randrange(len(self.random_pos) - n) + n
            self.swap_random_(n, nRndPos)
            info = self.map_info[self.random_pos[n]]
            if not info.is_terrible():
                cur_peer_info = info.peer_info
                cur_peer_info.timestamp = max(
                    cur_peer_info.timestamp,
                    info.nTime
                )
                addr.append(cur_peer_info)

        return addr

    def connect_(self, addr, nTime):
        info, _ = self.find_(addr)
        if info is None:
            return

        # check whether we are talking about the exact same peer
        if not (
            info.peer_info.host == addr.host
            and info.peer_info.port == addr.port
        ):
            return

        update_interval = 20 * 60
        if nTime - info.nTime > update_interval:
            info.nTime = nTime

    async def size(self):
        async with self.lock:
            return len(self.random_pos)

    async def add_to_new_table(self, addresses, source=None, penalty=0):
        is_added = False
        async with self.lock:
            for addr in addresses:
                is_added = is_added or self.add_to_new_table_(addr, source, penalty)
        return is_added

    # Mark an entry as accesible.
    async def mark_good(self, addr, test_before_evict=True, nTime=time.time()):
        async with self.lock:
            self.mark_good_(addr, test_before_evict, nTime)

    # Mark an entry as connection attempted to.
    async def attempt(self, addr, count_failures, nTime=time.time()):
        async with self.lock:
            self.attempt_(addr, count_failures, nTime)

    # See if any to-be-evicted tried table entries have been tested and if so resolve the collisions.
    async def resolve_tried_collisions(self):
        async with self.lock:
            self.resolve_tried_collisions_()

    # Randomly select an address in tried that another address is attempting to evict.
    async def select_tried_collision(self):
        async with self.lock:
            return self.select_tried_collision_()

    # Choose an address to connect to.
    async def select_peer(self, new_only=False):
        async with self.lock:
            return self.select_peer_(new_only)

    # Return a bunch of addresses, selected at random.
    async def get_peers(self):
        async with self.lock:
            return self.get_peers_()

    async def connect(self, addr, nTime=time.time()):
        async with self.lock:
            return self.connect_(addr, nTime)

    # Serialized format:
    # * nKey
    # * new_count
    # * tried_count
    # * number of "new" buckets
    # * all new_count addrinfos in new_matrix
    # * all tried_count addrinfos in tried_matrix
    # * for each bucket:
    # * * number of elements
    # * * for each element: index
    
    # Notice that tried_matrix, map_addr and vVector are never encoded explicitly;
    # they are instead reconstructed from the other information.
    #
    # new_matrix is serialized, but only used if ADDRMAN_UNKNOWN_BUCKET_COUNT didn't change,
    # otherwise it is reconstructed as well.
    #
    # This format is more complex, but significantly smaller (at most 1.5 MiB), and supports
    # changes to the ADDRMAN_ parameters without breaking the on-disk structure.

    async def serialize(self, filename):
        async with self.lock:
            with open(filename, 'w') as writer:
                writer.write(str(self.nKey) + "\n")
                writer.write(str(self.new_count) + "\n")
                writer.write(str(self.tried_count) + "\n")
                writer.write(str(NEW_BUCKET_COUNT) + "\n")
                unique_ids = {}
                count_ids = 0

                for node_id, info in self.map_info.items():
                    unique_ids[node_id] = count_ids
                    if info.ref_count > 0:
                        assert count_ids != self.new_count
                        writer.write(info.to_string() + "\n")
                        count_ids += 1

                count_ids = 0
                for node_id, info in self.map_info.items():
                    if info.is_tried:
                        assert count_ids != self.tried_count
                        writer.write(info.to_string() + "\n")
                        count_ids += 1

                for bucket in range(NEW_BUCKET_COUNT):
                    bucket_size = 0
                    for i in range(BUCKET_SIZE):
                        if self.new_matrix[bucket][i] != -1:
                            bucket_size += 1
                    writer.write(str(bucket_size) + "\n")
                    for i in range(BUCKET_SIZE):
                        if self.new_matrix[bucket][i] != -1:
                            index = unique_ids[self.new_matrix[bucket][i]]
                            writer.write(str(index) + "\n")

    async def unserialize(self, filename):
        await self.clear()
        async with self.lock:
            with open(filename, 'r') as reader:
                self.nKey = int(reader.readline())
                self.new_count = int(reader.readline())
                self.tried_count = int(reader.readline())
                buckets = int(reader.readline())
                assert buckets == NEW_BUCKET_COUNT
                assert self.new_count <= NEW_BUCKET_COUNT * BUCKET_SIZE
                assert self.tried_count <= TRIED_BUCKET_COUNT * BUCKET_SIZE
                for n in range(self.new_count):
                    info = ExtendedPeerInfo.from_string(reader.readline())
                    self.map_addr[info.peer_info.host] = n
                    self.map_info[n] = info
                    info.random_pos = len(self.random_pos)
                    self.random_pos.append(n)
                lost_count = 0
                id_count = self.new_count

                for n in range(self.tried_count):
                    info = ExtendedPeerInfo.from_src(reader.readline())
                    tried_bucket = info.get_tried_bucket(self.nKey)
                    tried_bucket_pos = info.get_bucket_position(self.nKey, False, tried_bucket)
                    if self.tried_matrix[tried_bucket][tried_bucket_pos] == -1:
                        info.random_pos = len(self.random_pos)
                        info.is_tried = True
                        self.random_pos.append(id_count)
                        self.map_info[id_count] = info
                        self.map_addr[info.peer_info.host] = id_count
                        self.tried_matrix[tried_bucket][tried_bucket_pos] = id_count
                        id_count += 1
                    else:
                        lost_count += 1
                self.tried_count -= lost_count

                for bucket in range(NEW_BUCKET_COUNT):
                    bucket_size = int(reader.readline())
                    for n in range(bucket_size):
                        index = int(reader.readline())
                        if (index >= 0 and index < self.new_count):
                            info = self.map_info[index]
                            bucket_pos = info.get_bucket_position(self.nKey, True, bucket)
                            if (
                                self.new_matrix[bucket][bucket_pos] == -1
                                and info.ref_count < NEW_BUCKETS_PER_ADDRESS
                            ):
                                info.ref_count += 1
                                self.new_matrix[bucket][bucket_pos] = index
                for node_id, info in self.map_info:
                    if (
                        info.is_tried == False
                        and info.ref_count == 0
                    ):
                        self.delete_new_entry_(node_id)
