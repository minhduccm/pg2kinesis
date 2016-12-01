import psycopg2
import psycopg2.extensions
import psycopg2.errorcodes
from collections import namedtuple

from psycopg2.extras import NamedTupleCursor

from .log import logger

psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)

PrimaryKeyMapItem = namedtuple('PrimaryKeyMapItem', 'table_name, col_name, col_type, col_ord_pos')


class SlotReader(object):
    PK_SQL = u"""SELECT CONCAT(table_schema, '.', table_name), column_name, data_type, ordinal_position
                 FROM information_schema.tables
                 LEFT JOIN (
                     SELECT CONCAT(table_schema, '.', table_name), column_name, data_type, c.ordinal_position,
                                 table_catalog, table_schema, table_name
                     FROM information_schema.table_constraints
                     JOIN information_schema.key_column_usage AS kcu
                         USING (constraint_catalog, constraint_schema, constraint_name,
                                     table_catalog, table_schema, table_name)
                     JOIN information_schema.columns AS c
                         USING (table_catalog, table_schema, table_name, column_name)
                     WHERE constraint_type = 'PRIMARY KEY'
                 ) as q using (table_catalog, table_schema, table_name)
                 ORDER BY ordinal_position;"""

    def __init__(self, database, host, port, user, slot_name):
        # Cool fact: using connections as context manager doesn't close them on success after leaving with block
        self._db_confg = dict(database=database, host=host, port=port, user=user)
        self._repl_conn = self._get_connection(connection_factory=psycopg2.extras.LogicalReplicationConnection)
        self._conn = self._get_connection()
        self._cursor = self._repl_conn.cursor()

        self.slot_name = slot_name
        self.cur_lag = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cursor.close()
        self._repl_conn.close()
        self._conn.close()

    def _get_connection(self, connection_factory=None, cursor_factory=None):
        return psycopg2.connect(connection_factory=connection_factory, cursor_factory=cursor_factory, **self._db_confg)

    def _execute_and_fetch(self, sql, *params):

        with self._conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)

            return cur.fetchall()

    def primary_key_map(self):
        logger.info('Getting primary key map')
        result = map(PrimaryKeyMapItem._make, self._execute_and_fetch(SlotReader.PK_SQL))
        pk_map = {rec.table_name: rec for rec in result}

        # generator that returns tuples of table_name, primary_key_col
        return pk_map

    def create_slot(self):
        logger.info('Creating slot %s' % self.slot_name)
        try:
            self._cursor.create_replication_slot(self.slot_name,
                                                 slot_type=psycopg2.extras.REPLICATION_LOGICAL,
                                                 output_plugin='test_decoding')
        except psycopg2.ProgrammingError as p:
            # Will be raised if slot exists already.
            if p.pgcode != psycopg2.errorcodes.DUPLICATE_OBJECT:
                logger.error(p)
                raise
            else:
                logger.info('Slot %s is already present.' % self.slot_name)

    def delete_slot(self):
        logger.info('Deleting slot %s' % self.slot_name)
        try:
            self._cursor.drop_replication_slot(self.slot_name)
        except psycopg2.ProgrammingError as p:
            # Will be raised if slot exists already.
            if p.pgcode != psycopg2.errorcodes.UNDEFINED_OBJECT:
                logger.error(p)
                raise
            else:
                logger.info('Slot %s was not found.' % self.slot_name)

    def process_replication_stream(self, consume):
        self._cursor.start_replication(self.slot_name)
        self._cursor.consume_stream(consume)