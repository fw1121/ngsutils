#!/usr/bin/env python
## category DNA-seq
## desc Base caller
"""
Base caller

Given a BAM file and a genomic reference, for each position covered in the
BAM file, show the reference base, and the number of A/T/C/G's and InDels.

You can also optionally filter out all bases whose quality score is below a
threshold, bases that aren't covered by enough reads, bases that have no
variation compared to reference, or bases whose variation is too low.

The output is a tab-delimited file that contains the following for each base:

chromosome
position (1-based)
reference base
# reads that contain this base
Consensus call
Minor call
Average mappings (number of mappings each read covering this base has)
(optional heterozygosity p-value)
Entropy
# A calls
# C calls
# G calls
# T calls
# deletions
# gaps
# inserts

If -hettest is applied, a Fisher test is performed to see if the base calls
likely indicate a heterozygous call. The Fisher table is setup like this:

                                 Major call     |    Minor call
                            -----------------------------------------
Theoretical homozygous call   total-background  |  background count
Actual calls                  actual top call   |  actual 2nd call

So if the call breakdown was A:10, C:2, G:1, T:0, A is the top call, C is the
2nd (minor) call, G is the background level, and T is ignored. The Fisher
table then looks like this:

                 major  | minor
                ----------------
Theoretical     13 - 1  |   1
Actual            10    |   2

And the p-value is: 0.373 (not significant)

If -showstrand is applied, a minor strand percentage is also calculated.p This
is calculated as:
    pct = (# reads with base on plus/ # reads with base total)
    if pct > 0.5,
        pct = 1-pct

Entropy is sum(a..t) { p log2 p } where p = freq(+pseudocount) / genomic freq.
pseudo count = genomic freq * sqrt(N)

We use the following genomic frequencies: A 0.3, C 0.2, G 0.2, T 0.3
"""

import os
import sys
import math
import collections
import datetime
from support.eta import ETA
import pysam
from support.ngs_utils import memoize


try:
    import scipy.stats
    SCIPY_PRESENT = True
except:
    SCIPY_PRESENT = False


def usage():
    print __doc__
    print """
Usage: bamutils basecall {opts} in.bam {chrom:start-end}

Options:
-ref   fname   Include reference basecalls from this file
-qual  val     Minimum base-quality level to use in calculations
               (numeric, Sanger scale) (default 0)

-count val     Report only bases with this minimum number of read-coverage
               (matches, inserts, deletions counted) (default 0)

-mask  val     The bitmask to use for filtering reads by flag
               (default 1540 - see SAM format for details)

-minorpct pct  Require a minor call to be within [pct] percent of the
               consensus call. Calculated as #minor / #consensus.
               (0.0 -> 1.0, default 0.01)

-hettest       Add a column to assign a p-value to assess the heterozygosity
               of each base. (Based on Fisher's exact test for a theoretical
               homozygous call) (requires scipy, experimental)

-showgaps      Report gaps/splice-junctions in RNA-seq data

-showstrand    Show the minor-strand percentages for each call
               (0-0.5 only shows the minor strand %)

-bed fname     Only output positions that are present in this BED file
               (*must* be sorted and reduced with the -nostrand option)
"""
    sys.exit(1)

__genomic_freq = {'A': 0.3, 'C': 0.2, 'G': 0.2, 'T': 0.3}


@memoize
def calc_entropy(a, c, t, g):
    counts = {'A': a, 'C': c, 'G': g, 'T': t}

    N = counts['A'] + counts['C'] + counts['G'] + counts['T']
    if N == 0:
        return 0

    N_sqrt = math.sqrt(N)

    count_pseudo = {}
    N_pseudo = 0

    for base in 'ATCG':
        count_pseudo[base] = counts[base] + (__genomic_freq[base] * N_sqrt)
        N_pseudo += count_pseudo[base]

    acc = 0
    for base in 'ATCG':
        p = float(count_pseudo[base]) / N_pseudo / __genomic_freq[base]
        acc += (p * math.log(p, 2))

    return acc

MappingRecord = collections.namedtuple('MappingRecord', 'qpos cigar_op base qual read')
MappingPos = collections.namedtuple('MappingPos', 'tid pos records')
BasePosition = collections.namedtuple('BasePosition', 'tid pos total a c g t n deletions gaps insertions reads a_minor c_minor g_minor t_minor n_minor del_minor ins_minor')


class BamBaseCaller(object):
    def __init__(self, bam_fname, min_qual=0, min_count=0, regions=None, mask=1540, quiet=False):
        self.bam = pysam.Samfile(bam_fname, 'rb')
        self.min_qual = min_qual
        self.min_count = 0

        self.regions = regions
        self.cur_chrom = None
        self.cur_start = None
        self.cur_end = None

        self.mask = mask
        self.quiet = quiet

        def _gen1():
            if not self.quiet:
                eta = ETA(self.regions.total)
            else:
                eta = None

            count = 0
            for chrom, start, end in self.regions.regions:
                if chrom in self.bam.references:
                    self.cur_chrom = chrom
                    self.cur_start = start
                    self.cur_end = end

                    laststart = 0
                    for read in self.bam.fetch(chrom, start, end):
                        if read.pos != laststart:
                            count += 1
                            laststart = read.pos

                        if eta:
                            eta.print_status(count, extra='%s/%s %s:%s' % (count, self.regions.total, self.bam.references[read.tid], read.pos))

                        yield read
            if eta:
                eta.done()

        def _gen2():
            if not self.quiet:
                eta = ETA(0, bamfile=self.bam)
            else:
                eta = None

            for read in self.bam:
                if eta:
                    eta.print_status(extra='%s:%s (%s) %s:%s-%s' % (self.bam.references[read.tid], read.pos, len(self.buffer), self.cur_chrom, self.cur_start, self.cur_end), bam_pos=(read.tid, read.pos))
                yield read

            if eta:
                eta.done()

        if regions:
            self._gen = _gen1
        else:
            self._gen = _gen2

        self.buffer = None
        self.current_tid = None

    def close(self):
        self.bam.close()

    def _calc_pos(self, tid, pos, records):
        if self.cur_start and pos < self.cur_start:
            return None
        if self.cur_end and self.cur_end < pos:
            return None

        counts = {'A': 0, 'C': 0, 'G': 0, 'T': 0, 'N': 0, 'ins': 0, 'del': 0}
        plus_counts = {'A': 0, 'C': 0, 'G': 0, 'T': 0, 'N': 0, 'ins': 0, 'del': 0}

        insertions = {}
        gaps = 0
        total = 0
        reads = []

        for record in records:
            qpos, cigar_op, base, qual, read = record
            if cigar_op == 0:  # M
                if qual >= self.min_qual and (read.flag & self.mask) == 0:
                    total += 1
                    reads.append(record)

                    counts[base] += 1
                    if not read.is_reverse:
                        plus_counts[base] += 1
            elif cigar_op == 1:  # I
                if qual >= self.min_qual and (read.flag & self.mask) == 0:
                    reads.append(record)

                    if not base in insertions:
                        insertions[base] = 1
                    else:
                        insertions[base] += 1

                        counts['ins'] += 1
                    if not read.is_reverse:
                        plus_counts['ins'] += 1
            elif cigar_op == 2:  # D
                #total += 1 # not sure these should be included,
                           # samtools mpileup includes them
                           # IGV doesn't

                counts['del'] += 1
                reads.append(record)
                if not read.is_reverse:
                    plus_counts['del'] += 1
            elif cigar_op == 3:  # N
                gaps += 1
                reads.append(record)

        pcts = {'A': 0.0, 'C': 0.0, 'G': 0.0, 'T': 0.0, 'N': 0.0, 'ins': 0.0, 'del': 0.0}
        for k in pcts:
            if counts[k] > 0:
                pcts[k] = float(plus_counts[k]) / counts[k]

                if pcts[k] > 0.5:
                    pcts[k] = 1 - pcts[k]

        if total >= self.min_count:
            return BasePosition(tid, pos, total, counts['A'], counts['C'], counts['G'], counts['T'], counts['N'], counts['del'], gaps, insertions, reads, pcts['A'], pcts['C'], pcts['G'], pcts['T'], pcts['N'], pcts['del'], pcts['ins'])

    def fetch(self):
        self.current_tid = None
        self.buffer = collections.deque()

        for read in self._gen():
            if self.current_tid != read.tid:  # new chromosome
                while self.buffer:
                    tid, pos, records = self.buffer.popleft()
                    y = self._calc_pos(tid, pos, records)
                    if y:
                        yield y

                self.current_tid = read.tid

            # handle all positions that are 5' of the current one
            while self.buffer and read.pos > self.buffer[0].pos:
                tid, pos, records = self.buffer.popleft()
                y = self._calc_pos(tid, pos, records)
                if y:
                    yield y

            self._push_read(read)

        # flush buffer for the end
        while self.buffer:
            tid, pos, records = self.buffer.popleft()
            y = self._calc_pos(tid, pos, records)
            if y:
                yield y

    def _push_read(self, read):
        if not self.buffer:
            self.buffer.append(MappingPos(read.tid, read.pos, []))

        while self.buffer[-1].pos < read.aend:
            self.buffer.append(MappingPos(read.tid, self.buffer[-1].pos + 1, []))

        buf_idx = 0
        while self.buffer[buf_idx].pos < read.pos:
            buf_idx += 1

        read_idx = 0
        for op, length in read.cigar:
            if op == 0:  # M
                for i in xrange(length):
                    try:
                        self.buffer[buf_idx].records.append(MappingRecord(read_idx, op, read.seq[read_idx], read.qual[read_idx], read))
                    except Exception, e:
                        sys.stderr.write('\n%s\nIf there is a BED file, is it sorted and reduced?\n' % e)
                        sys.stderr.write('read: %s (%s:%s-%s)\n' % (read.qname, self.bam.references[read.tid], read.pos, read.apos))
                        if self.cur_chrom:
                            sys.stderr.write('current range: %s:%s-%s\n' % (self.cur_chrom, self.cur_start, self.cur_end))
                        sys.exit(1)
                    buf_idx += 1
                    read_idx += 1

            elif op == 1:  # I
                inseq = ''
                inqual = 0
                for i in xrange(length):
                    inseq += read.seq[read_idx]
                    inqual += ord(read.qual[read_idx]) - 33
                    read_idx += 1

                inqual = inqual / len(inseq)

                self.buffer[buf_idx].records.append(MappingRecord(read_idx, op, inseq, inqual, read))

            elif op == 2:  # D
                mr = MappingRecord(read_idx, op, None, None, read)
                for i in xrange(length):
                    self.buffer[buf_idx].records.append(mr)
                    buf_idx += 1

            elif op == 3:  # N
                mr = MappingRecord(read_idx, op, None, None, read)
                for i in xrange(length):
                    self.buffer[buf_idx].records.append(mr)
                    buf_idx += 1


def _calculate_consensus_minor(minorpct, a, c, g, t):
    consensuscalls = []
    minorcalls = []

    calls = [(a, 'A'), (c, 'C'), (g, 'G'), (t, 'T')]
    calls.sort()
    calls.reverse()

    best = calls[0][0]
    minor = 0

    for count, base in calls:
        if count == 0:
            break
        if count == best:
            consensuscalls.append(base)
        elif not minor:
            minor = count
            minorcalls.append(base)
        elif count == minor:
            minorcalls.append(base)
        else:
            # background
            pass

    if best == 0:
        return ('N', '')

    if best and (float(minor) / best) < minorpct:
        minorcalls = []

    # if there is one major, there can be more than one minor
    # however, if there is more than one major, there are *no* minors
    #

    if len(consensuscalls) == 1:
        return (consensuscalls[0], '/'.join(minorcalls))
    return ('/'.join(consensuscalls), '')


@memoize
def _calculate_heterozygosity(a, c, g, t):
    total = a + c + g + t
    calls = [a, c, g, t]
    calls.sort()
    major = calls[-1]
    minor = calls[-2]
    background = calls[-3]

    if minor == 0:
        return 1.0  # There is no minor call, so not heterozygous!

    # Fisher test
    theoretical_major = total - background
    theoretical_minor = background

    oddsratio, pval = scipy.stats.fisher_exact([[theoretical_major, theoretical_minor], [major, minor]])
    return pval


def bam_basecall(bam_fname, ref_fname, min_qual=0, min_count=0, regions=None, mask=1540, quiet=False, showgaps=False, showstrand=False, minorpct=0.01, hettest=False, profiler=None):
    if ref_fname:
        ref = pysam.Fastafile(ref_fname)
    else:
        ref = None

    sys.stdout.write('chrom\tpos\tref\tcount\tconsensus call\tminor call\tave mappings')
    if hettest:
        sys.stdout.write('\theterozygousity')
    sys.stdout.write('\tentropy\tA\tC\tG\tT\tN\tDeletions\tGaps\tInsertions\tInserts')

    if showstrand:
        sys.stdout.write('\t+ strand %\tA minor %\tC minor %\tG minor %\tT minor %\tN minor %\tDeletion minor %\tInsertion minor %')

    sys.stdout.write('\n')

    bbc = BamBaseCaller(bam_fname, min_qual, min_count, regions, mask, quiet)
    for basepos in bbc.fetch():
        if profiler and profiler.abort():
            break

        big_total = basepos.total + basepos.deletions + len(basepos.insertions)

        if big_total < min_count:
            continue

        if big_total == 0 and not (showgaps and basepos.gaps > 0):
            continue

        if ref:
            refbase = ref.fetch(bbc.bam.references[basepos.tid], basepos.pos, basepos.pos + 1).upper()
        else:
            refbase = 'N'

        entropy = calc_entropy(basepos.a, basepos.c, basepos.g, basepos.t)

        read_ih_acc = 0
        plus_count = 0.0  # needs to be float
        total_count = 0
        for qpos, cigar_op, base, qual, read in basepos.reads:
            total_count += 1
            if not read.is_reverse:
                plus_count += 1.0
            if cigar_op in [0, 1, 2]:
                try:
                    read_ih_acc += int(read.opt('IH'))
                except KeyError:
                    read_ih_acc += 1

        inserts = []
        for insert in basepos.insertions:
            inserts.append((basepos.insertions[insert], insert))
        inserts.sort()
        inserts.reverse()

        insert_str_ar = []
        incount = 0
        for count, insert in inserts:
            insert_str_ar.append('%s:%s' % (insert, count))
            incount += count

        if big_total > 0:
            ave_mapping = (float(read_ih_acc) / big_total)
        else:
            ave_mapping = 0

        consensuscall, minorcall = _calculate_consensus_minor(minorpct, basepos.a, basepos.c, basepos.g, basepos.t)

        cols = [bbc.bam.references[basepos.tid],
                 basepos.pos + 1,
                 refbase,
                 basepos.total,
                 consensuscall,
                 minorcall,
                 ave_mapping,
                 ]

        if hettest:
            cols.append(_calculate_heterozygosity(basepos.a, basepos.c, basepos.g, basepos.t))

        cols.extend([
                 entropy,
                 basepos.a,
                 basepos.c,
                 basepos.g,
                 basepos.t,
                 basepos.n,
                 basepos.deletions,
                 basepos.gaps,
                 incount,
                 ','.join(insert_str_ar)])

        if showstrand:
            cols.append(plus_count / total_count)
            cols.append(basepos.a_minor)
            cols.append(basepos.c_minor)
            cols.append(basepos.g_minor)
            cols.append(basepos.t_minor)
            cols.append(basepos.n_minor)
            cols.append(basepos.del_minor)
            cols.append(basepos.ins_minor)

        sys.stdout.write('%s\n' % '\t'.join([str(x) for x in cols]))

    bbc.close()
    if ref:
        ref.close()


class SingleRegion(object):
    def __init__(self, arg):
        self.chrom, startend = arg.split(':')
        if '-' in startend:
            self.start, self.end = [int(x) for x in startend.split('-')]
        else:
            self.start = int(startend)
            self.end = start
        self.start = self.start - 1

    @property
    def total(self):
        return end - start

    @property
    def regions(self):
        yield (chrom, start, end)


class BEDRegions(object):
    def __init__(self, fname):
        self.fname = fname
        self.__total = 0

    @property
    def total(self):
        if not self.__total:
            self.__total = 0
            with open(self.fname) as f:
                for line in f:
                    if line[0] == '#':
                        continue
                    chrom, start, end, name, score, strand = line.strip().split('\t')
                    self.__total += (int(end) - int(start))
        return self.__total

    @property
    def regions(self):
        with open(self.fname) as f:
            for line in f:
                if line[0] == '#':
                    continue
                chrom, start, end, name, score, strand = line.strip().split('\t')
                yield (chrom, int(start), int(end))


class TimedProfiler(object):
    def __init__(self, secs_to_run=3600):  # default is to run for one hour
        self.expire_ts = datetime.datetime.now() + datetime.timedelta(seconds=secs_to_run)

    def abort(self):
        if datetime.datetime.now() > self.expire_ts:
            return True
        return False

if __name__ == '__main__':
    bam = None
    ref = None

    min_qual = 0
    min_count = 0
    mask = 1540
    chrom = None
    start = None
    end = None
    quiet = False
    showgaps = False
    showstrand = False
    hettest = False
    minorpct = 0.01
    regions = None

    profile = None

    last = None
    try:
        for arg in sys.argv[1:]:
            if last == '-qual':
                min_qual = int(arg)
                last = None
            elif last == '-ref':
                if os.path.exists(arg) and os.path.exists('%s.fai' % arg):
                    ref = arg
                else:
                    print "Missing FASTA file or index: %s" % arg
                    usage()
                last = None
            elif last == '-count':
                min_count = int(arg)
                last = None
            elif last == '-bed':
                if os.path.exists(arg):
                    regions = BEDRegions(arg)
                else:
                    print "BED file: %s not found!" % arg
                    usage()
                last = None
            elif last == '-mask':
                mask = int(arg)
                last = None
            elif last == '-minorpct':
                minorpct = float(arg)
                last = None
            elif last == '-profile':
                profile = arg
                last = None
            elif arg == '-h':
                usage()
            elif arg == '-showstrand':
                showstrand = True
            elif arg == '-showgaps':
                showgaps = True
            elif arg == '-q':
                quiet = True
            elif arg == '-hettest':
                if not SCIPY_PRESENT:
                    print "-hettest requires scipy to be installed"
                    usage()
                hettest = True
            elif arg in ['-qual', '-count', '-mask', '-ref', '-minorpct', '-profile', '-bed']:
                last = arg
            elif not bam and os.path.exists(arg):
                if os.path.exists('%s.bai' % arg):
                    bam = arg
                else:
                    print "Missing BAI index on %s" % arg
                    usage()
            elif not ref and os.path.exists(arg) and os.path.exists('%s.fai' % arg):
                if os.path.exists('%s.fai' % arg):
                    ref = arg
                else:
                    print "Missing FAI index on %s" % arg
                    usage()
            elif not regions:
                regions = SingleRegion(arg)
            else:
                print "Unknown option or missing index: %s" % arg
                usage()
    except Exception, e:
        print e
        usage()

    if not bam:
        usage()
    else:
        if profile:
            import cProfile

            def func():
                bam_basecall(bam, ref, min_qual, min_count, regions, mask, quiet, showgaps, showstrand, minorpct, hettest, TimedProfiler())
            sys.stderr.write('Profiling...\n')
            cProfile.run('func()', profile)
        else:
                bam_basecall(bam, ref, min_qual, min_count, regions, mask, quiet, showgaps, showstrand, minorpct, hettest, None)
