import { CompactTable } from './connector';
import { IConnector } from './connector';
import { getLogger } from '@server/utils';

import pg from 'pg';
const { Client } = pg;

const logger = getLogger('PGConnector');
logger.level = 'debug';

export interface PGConnectionConfig {
  user: string;
  password: string;
  host: string;
  database: string;
  port: number;
}

export interface PGColumnResponse {
  table_catalog: string;
  table_schema: string;
  table_name: string;
  column_name: string;
  ordinal_position: string;
  is_nullable: string;
  data_type: string;
}

export interface PGConstraintResponse {
  constraintName: string;
  constraintType: string;
  constraintTable: string;
  constraintColumn: string;
  constraintedTable: string;
  constraintedColumn: string;
}

export interface PGListTableOptions {
  format?: boolean;
}

export class PGConnector
  implements IConnector<PGColumnResponse, PGConstraintResponse>
{
  private config: PGConnectionConfig;
  private client?: pg.Client;

  constructor(config: PGConnectionConfig) {
    this.config = config;
  }

  public async prepare() {
    return;
  }

  public async connect(): Promise<boolean> {
    try {
      await this.prepareClient();
      // query to check if connection is successful
      await this.client.query('SELECT 1;');
      return true;
    } catch (err) {
      logger.error(`Error connecting to PG: ${err}`);
      return false;
    }
  }

  public async listTables(options: PGListTableOptions) {
    const sql = `
      SELECT
        t.table_catalog,
        t.table_schema,
        t.table_name,
        c.column_name,
        c.data_type,
        c.is_nullable,
        c.ordinal_position
      FROM
        information_schema.tables t
      JOIN
        information_schema.columns c ON t.table_schema = c.table_schema AND t.table_name = c.table_name
      WHERE
        t.table_type in ('BASE TABLE', 'VIEW')
        and t.table_schema not in ('information_schema', 'pg_catalog')
      ORDER BY
        t.table_schema,
        t.table_name,
        c.ordinal_position;
    `;
    await this.prepareClient();
    const res = await this.client.query(sql);
    const columns = res.rows.map((row) => {
      return {
        table_catalog: row.table_catalog,
        table_schema: row.table_schema,
        table_name: row.table_name,
        column_name: row.column_name,
        ordinal_position: row.ordinal_position,
        is_nullable: row.is_nullable,
        data_type: row.data_type,
      };
    }) as PGColumnResponse[];

    return options.format ? this.formatToCompactTable(columns) : columns;
  }

  public async listConstraints() {
    const sql = `
      SELECT
        tc.table_schema,
        tc.constraint_name,
        tc.table_name,
        kcu.column_name,
        ccu.table_schema AS foreign_table_schema,
        ccu.table_name AS foreign_table_name,
        ccu.column_name AS foreign_column_name
      FROM information_schema.table_constraints AS tc
      JOIN information_schema.key_column_usage AS kcu
        ON tc.constraint_name = kcu.constraint_name
        AND tc.table_schema = kcu.table_schema
      JOIN information_schema.constraint_column_usage AS ccu
        ON ccu.constraint_name = tc.constraint_name
      WHERE tc.constraint_type = 'FOREIGN KEY'
    `;
    await this.prepareClient();
    const res = await this.client.query(sql);
    const constraints = res.rows.map((row) => {
      return {
        constraintName: row.constraint_name,
        constraintType: 'FOREIGN KEY',
        constraintTable: row.table_name,
        constraintColumn: row.column_name,
        constraintedTable: row.foreign_table_name,
        constraintedColumn: row.foreign_column_name,
      };
    }) as PGConstraintResponse[];
    return constraints;
  }

  private formatToCompactTable(columns: PGColumnResponse[]): CompactTable[] {
    return columns.reduce((acc: CompactTable[], row: PGColumnResponse) => {
      const {
        table_catalog,
        table_schema,
        table_name,
        column_name,
        is_nullable,
        data_type,
      } = row;
      let table = acc.find(
        (t) => t.name === table_name && t.properties.schema === table_schema,
      );
      if (!table) {
        table = {
          name: table_name,
          description: '',
          columns: [],
          properties: {
            schema: table_schema,
            catalog: table_catalog,
          },
        };
        acc.push(table);
      }
      table.columns.push({
        name: column_name,
        type: data_type,
        notNull: is_nullable.toLocaleLowerCase() !== 'yes',
        description: '',
        properties: {},
      });
      return acc;
    }, []);
  }

  public async close() {
    if (this.client) {
      await this.client.end();
      this.client = null;
    }
  }

  private async prepareClient() {
    if (this.client) {
      return;
    }

    this.client = new Client(this.config);
    await this.client.connect();
  }
}