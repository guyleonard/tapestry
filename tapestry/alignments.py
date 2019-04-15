import os
import pysam
import logging as log
import pandas as pd

from collections import namedtuple

from sqlalchemy import create_engine, MetaData, Table, Column, ForeignKey, func
from sqlalchemy import Integer, String, Boolean
from sqlalchemy.sql import select, and_

from .misc import file_exists


class Alignments():
    def __init__(self, db_filename):
        self.db_filename = db_filename
        self.engine = create_engine(f'sqlite:///{db_filename}')
        self.metadata = MetaData(self.engine)
        self.reads, self.contigs, self.ranges, self.alignments = self.tables()


    def windowsize_matches(self):
        ws_matches = select([func.count(self.ranges.c.width)]).where(self.ranges.c.width == self.windowsize)
        ws_all =     select([func.count(self.ranges.c.width)])
        
        with self.engine.connect() as conn:
            matches = conn.execute(ws_matches).fetchall()
            windows = conn.execute(ws_all).fetchall()
        
        # Hack; 50% of windows in the database are the same size as the window size option.
        # The only windows that aren't this size are at the end of the contig, so can't do equality check,
        # but should be much more than 50% matching. However, 50% should be sufficient
        return matches[0][0] > (windows[0][0] * 0.5)


    def load(self, reads_bam, contigs_bam, reference, windowsize):
        self.windowsize = windowsize

        db_exists = file_exists(self.db_filename, deps=[reads_bam, contigs_bam])
        if db_exists and self.windowsize_matches():
            log.info(f"Will use existing {self.db_filename}")
        else:
            log.info(f"Building alignments database {self.db_filename}")
            if file_exists(self.db_filename): # If DB exists but is older than BAMs,
                os.remove(self.db_filename)   # delete it, so new records aren't loaded into it

            self.metadata.create_all(self.engine)

            self.load_reference(reference)
            self.load_alignments(contigs_bam, 'contig')
            self.load_alignments(reads_bam, 'read')


    def tables(self):
        return [
            Table('reads', self.metadata,
                Column('name', String, primary_key=True),
                Column('length', Integer)
            ),
            Table('contigs', self.metadata,
                Column('name', String, primary_key=True),
                Column('length', Integer)
            ),
            Table('ranges', self.metadata,
                Column('contig', String, ForeignKey('contigs.name')),
                Column('width', Integer),
                Column('start', Integer),
                Column('end', Integer)
            ),
            Table('alignments', self.metadata,
                Column('query', Integer),
                Column('querytype', String),
                Column('alntype', Integer),
                Column('contig', String, ForeignKey('contigs.name')),
                Column('mq', Integer),
                Column('reversed', Boolean),
                Column('ref_start', Integer),
                Column('ref_end', Integer),
                Column('ref_length', Integer),
                Column('query_start', Integer),
                Column('query_end', Integer),
                Column('aligned_length', Integer),
                Column('left_clip', Integer),
                Column('right_clip', Integer),
            )
        ]


    def load_reference(self, reference):
        try:
            with self.engine.connect() as conn:
                contig_rows = []
                ranges_rows = []
                for contig in reference:
                    contig_length = len(reference[contig])
                    contig_rows.append({'name'  : reference[contig].name,
                                        'length': contig_length})

                    for start in range(1, contig_length, int(self.windowsize/2)):
                        end = min(start + self.windowsize - 1, contig_length)
            
                        ranges_rows.append({'contig' : reference[contig].name,
                                           'width'  : end - start + 1,
                                           'start'  : start,
                                           'end'    : end})
                        if end == contig_length: # Skip remaining windows if last window already reaches end of contig
                            break
            
                conn.execute(self.contigs.insert(), contig_rows)
                conn.execute(self.ranges.insert(), ranges_rows)
        except:
            log.error(f"Failed to add assembly to alignments database {self.db_filename}")


    def load_alignments(self, bam_filename, query_type=None):
        if not file_exists(bam_filename):
            log.error(f"No up-to-date {bam_filename} file, will not process {query_type} alignments")
            return

        try:
            with self.engine.connect() as conn:
                for alignment_chunk, reads_chunk in self.process_bam_chunks(bam_filename, query_type):
                    conn.execute(self.alignments.insert(), alignment_chunk)
                    if reads_chunk:
                        conn.execute(self.reads.insert(), reads_chunk)
        except:
            log.error(f"Failed to add {query_type} alignments to database {self.db_filename}")


    def process_bam_chunks(self, bamfile, query_type, chunksize=1000):
        bam = pysam.AlignmentFile(bamfile, 'rb')
        alncount = 0
        read_names = {}
        reads_chunk = []
        alignment_chunk = []
        for aln in bam.fetch(until_eof=True): # until_eof includes unmapped reads
            alntype                      = self.get_alignment_type(aln)
            query_length, aligned_length = self.get_alignment_lengths(aln, alntype)

            query_start, query_end, left_clip, right_clip = self.get_query_ends(aln, alntype, query_length)
        
            if query_type is 'read' and aln.query_name not in read_names:
                read_names[aln.query_name] = True
                reads_chunk.append({'name':aln.query_name, 'length':query_length})

            alignment_chunk.append({
                'query':aln.query_name,
                'querytype':query_type,
                'alntype':alntype,
                'contig':aln.reference_name,
                'mq':aln.mapping_quality,
                'reversed':aln.is_reverse,
                'ref_start': aln.reference_start + 1, # BAM is 0-based
                'ref_end': aln.reference_end,
                'ref_length': aln.reference_length,
                'query_start': query_start,
                'query_end': query_end,
                'left_clip': left_clip,
                'right_clip': right_clip,
                'aligned_length': aligned_length
            })

            alncount += 1
            if alncount == 1000:
                yield alignment_chunk, []
                alncount = 0
                alignment_chunk = []

        yield alignment_chunk, reads_chunk


    def get_alignment_type(self, aln):
        alignment_type = 'primary'
        if aln.is_unmapped:
            alignment_type = 'unmapped'
        elif aln.is_secondary:
            alignment_type = 'secondary'
        elif aln.is_supplementary:
            alignment_type = 'supplementary'
        return alignment_type
    
    
    def get_alignment_lengths(self, aln, alntype):
        query_length = aln.infer_read_length()
        aligned_length = aln.query_alignment_length
        if alntype == 'unmapped':
            query_length = aln.query_length
            aligned_length = 0
        return query_length, aligned_length


    def get_query_ends(self, aln, alntype, query_length):
        query_start = query_end = None
        if alntype is 'unmapped':
            return query_start, query_end, None, None
        first_clip_length = self.get_clip_lengths(aln.cigartuples[0])
        last_clip_length  = self.get_clip_lengths(aln.cigartuples[-1])

        if aln.is_reverse:
            query_start = 1 + last_clip_length # Queries all start at 1
            query_end = query_length - first_clip_length
        else:
            query_start = 1 + first_clip_length
            query_end = query_length - last_clip_length

        return query_start, query_end, first_clip_length, last_clip_length


    def get_clip_lengths(self, cigartuple):
        cigar_type, cigar_length = cigartuple
        if cigar_type not in (4,5): # Not soft- or hard-clipped, so no clip length
            cigar_length = 0
        return cigar_length


    def contig_alignments(self, contig_name):
        stmt = (select([
                    self.alignments.c.ref_start,
                    self.alignments.c.ref_end,
                    self.alignments.c.query
                ])
                .where(and_(
                    self.alignments.c.querytype == 'contig',
                    self.alignments.c.contig == contig_name
                    ))
               )

        with self.engine.connect() as conn:
            results = conn.execute(stmt).fetchall()

        return results


    def get_contig_read_counts(self, contig_name):
        stmt = (select([
                    self.alignments.c.alntype, 
                    func.count(self.alignments.c.query).label('reads'), 
                    func.sum(self.alignments.c.aligned_length).label('aligned_length'),
                    func.sum(self.reads.c.length).label('read_length')
                ])
                .select_from(self.reads.join(self.alignments, self.reads.c.name == self.alignments.c.query))
                .where(and_(
                    self.alignments.c.querytype == 'read',
                    self.alignments.c.contig == contig_name
                    ))
                .group_by(self.alignments.c.alntype)
               )

        with self.engine.connect() as conn:
            results = conn.execute(stmt).fetchall()

        # Convert results to DataFrame
        count_bases = pd.DataFrame(results)
        if count_bases.empty:
            return None
        count_bases.columns =  results[0].keys()
        count_bases = count_bases.set_index('alntype')

        # Fill missing values
        for alntype in 'primary', 'secondary', 'supplementary':
            if alntype not in count_bases.index:
                count_bases.loc[alntype] = [0, 0, 0]

        return count_bases


#                    RegionStart        RegionEnd                   ReadStart <= RegionEnd ReadEnd >= RegionStart And
#   ReadStart ReadEnd                                               True                   False                  False
#   ReadStart                   ReadEnd                             True                   True                   True
#   ReadStart                                     ReadEnd           True                   True                   True
#                        ReadStart ReadEnd                          True                   True                   True
#                               ReadStart         ReadEnd           True                   True                   True
#                                                 ReadStart ReadEnd False                  True                   False

    def alignments_in_region(self, query, contig_name, query_type, region_start, region_end):
        return query.where(and_(
            self.alignments.c.contig.like(contig_name + "%"),
            self.alignments.c.querytype == query_type,
            self.alignments.c.ref_start <= region_end,
            self.alignments.c.ref_end   >= region_start
        ))


    def depths(self, query_type, contig_name=''):

        # Get read depths for each region
        rd = (select([
                self.ranges.c.contig, 
                self.ranges.c.start, 
                func.count(self.alignments.c.query).label('depth')
             ])
              .select_from(self.ranges.join(self.alignments, self.ranges.c.contig == self.alignments.c.contig))
              .where(and_(self.alignments.c.alntype.in_(["primary", "supplementary"]),
                         self.alignments.c.mq == 60)
                    )
             )

        rdf = self.alignments_in_region(rd, contig_name, query_type, self.ranges.c.start, self.ranges.c.end)

        # Group by regions and make alias for column reference below
        rdg = rdf.group_by(self.ranges.c.contig, self.ranges.c.start).alias()

        # Combine with ranges table again to fill empty regions
        stmt = (select([
                    self.ranges.c.contig, 
                    self.ranges.c.start, 
                    self.ranges.c.end, 
                    rdg.c.depth])
                .select_from(
                    self.ranges.outerjoin(rdg,
                        and_(self.ranges.c.contig == rdg.c.contig,
                             self.ranges.c.start == rdg.c.start)
                ))
                .where(self.ranges.c.contig.like(contig_name+"%"))
               )

        results = self.engine.connect().execute(stmt).fetchall()
        
        # Convert results to DataFrame
        depths = pd.DataFrame(results)
        if depths.empty:
            return None
        depths.columns =  results[0].keys()
        depths = depths.fillna(0).reset_index()

        return depths


    def get_start_overhangs(self, contig_name, region_start, region_end, aligned_length=0):
        stmt = (select([
                    region_start - (self.alignments.c.ref_start - self.alignments.c.left_clip)
                ])
                .where(and_(
                            self.alignments.c.ref_start.between(region_start, region_end),
                            self.alignments.c.contig.like(contig_name + "%"),
                            self.alignments.c.querytype == 'read',
                            self.alignments.c.aligned_length > aligned_length
                        ))
                )

        with self.engine.connect() as conn:
            results = conn.execute(stmt).fetchall()

        overhangs = [o[0] for o in results if o[0]>0]

        return overhangs


    def get_end_overhangs(self, contig_name, region_start, region_end, aligned_length=0):
        stmt = (select([
                    self.alignments.c.ref_end + self.alignments.c.right_clip - region_end
                ])
                .where(and_(
                            self.alignments.c.ref_end.between(region_start, region_end),
                            self.alignments.c.contig.like(contig_name + "%"),
                            self.alignments.c.querytype == 'read',
                            self.alignments.c.aligned_length > aligned_length
                        ))
                )

        with self.engine.connect() as conn:
            results = conn.execute(stmt).fetchall()

        overhangs = [o[0] for o in results if o[0]>0]

        return overhangs

    def read_alignments(self, contig):
        stmt = (select([
                self.alignments.c.ref_start,
                self.alignments.c.ref_end,
                self.alignments.c.left_clip,
                self.alignments.c.right_clip,
                self.alignments.c.mq,
                (self.alignments.c.ref_start - self.alignments.c.left_clip).label('start_position'),
                (self.alignments.c.ref_end + self.alignments.c.right_clip).label('end_position')
            ])
            .select_from(self.reads.join(self.alignments, self.reads.c.name == self.alignments.c.query))
            .where(and_(
                self.alignments.c.contig == contig,
                self.alignments.c.alntype != "secondary"
            ))
            .order_by("start_position")
        )

        with self.engine.connect() as conn:
            results = conn.execute(stmt).fetchall()

        alignments = pd.DataFrame(results)
        if alignments.empty:
            return None
        alignments.columns =  results[0].keys()
        return alignments