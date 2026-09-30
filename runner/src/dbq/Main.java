package dbq;

import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * dbq — on-device SQLite query runner, launched without an app Context via app_process.
 *
 *   adb exec-out run-as <pkg> sh -c 'CLASSPATH=code_cache/dbq.jar app_process / dbq.Main <base64 request>'
 *
 * Request (JSON, base64-encoded so it survives adb → sh → run-as quoting):
 *   {"db": "databases/app.db", "sql": "SELECT ...", "mode": "read" | "write", "limit": 200,
 *    "blobs": "summary" | "base64"}      (base64 → BLOB cells come back as {"$b64": "..."})
 *
 * Response (JSON on stdout):
 *   read : {"columns":[...], "rows":[[...],...], "row_count":n, "truncated":bool}
 *   write: {"ok":true, "changes":n}      mode "dryrun": same statement inside a rolled-back transaction
 *   ping : {"ok":true, "serve":true, "version":2}
 *   error: {"error":"..."}   (exit code 1 in one-shot mode)
 *
 * Serve mode: `dbq.Main --serve` reads one base64 request per stdin line and answers one JSON
 * line per request until EOF — avoids paying the ART start-up cost for every query.
 *
 * Runs as the app's own uid, uses the device's android.database.sqlite, so WAL, locks and
 * (with a key) SQLCipher-style wrappers behave exactly as they do for the app itself.
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length >= 1 && "--serve".equals(args[0])) {
            serve();
            return;
        }
        if (args.length < 1) {
            System.out.print("{\"error\":\"usage: dbq.Main <base64 json request> | --serve\"}");
            System.out.flush();
            System.exit(1);
        }
        String out = handle(args[0]);
        System.out.print(out);
        System.out.flush();
        System.exit(out.startsWith("{\"error\"") ? 1 : 0);
    }

    private static void serve() {
        try {
            java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(System.in, "UTF-8"));
            java.io.PrintStream out = new java.io.PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true, "UTF-8");
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                out.println(handle(line));
                out.flush();
            }
        } catch (Throwable t) {
            // stdin closed or broken pipe: exit quietly
        }
        System.exit(0);
    }

    private static String handle(String b64Request) {
        try {
            JSONObject req = new JSONObject(new String(Base64.decode(b64Request, Base64.DEFAULT), "UTF-8"));
            String mode = req.optString("mode", "read");
            if ("ping".equals(mode)) {
                JSONObject pong = new JSONObject();
                pong.put("ok", true);
                pong.put("serve", true);
                pong.put("version", 3);
                pong.put("dryrun", true);
                return pong.toString();
            }
            String db = req.getString("db");
            String sql = req.getString("sql");
            int limit = req.optInt("limit", 200);
            boolean b64 = "base64".equals(req.optString("blobs", "summary"));
            if ("write".equals(mode) || "dryrun".equals(mode)) {
                return write(db, sql, "dryrun".equals(mode)).toString();
            }
            return read(db, sql, limit, b64).toString();
        } catch (Throwable t) {
            JSONObject err = new JSONObject();
            try {
                err.put("error", t.getClass().getSimpleName() + ": " + t.getMessage());
            } catch (Exception ignored) {
            }
            return err.toString();
        }
    }

    private static JSONObject read(String path, String sql, int limit, boolean b64) throws Exception {
        SQLiteDatabase db = SQLiteDatabase.openDatabase(path, null, SQLiteDatabase.OPEN_READONLY);
        try {
            Cursor c = db.rawQuery(sql, null);
            try {
                JSONObject res = new JSONObject();
                JSONArray columns = new JSONArray();
                for (String name : c.getColumnNames()) {
                    columns.put(name);
                }
                JSONArray rows = new JSONArray();
                int n = 0;
                boolean truncated = false;
                while (c.moveToNext()) {
                    if (n >= limit) {
                        truncated = true;
                        break;
                    }
                    JSONArray row = new JSONArray();
                    for (int i = 0; i < c.getColumnCount(); i++) {
                        switch (c.getType(i)) {
                            case Cursor.FIELD_TYPE_NULL:
                                row.put(JSONObject.NULL);
                                break;
                            case Cursor.FIELD_TYPE_INTEGER:
                                row.put(c.getLong(i));
                                break;
                            case Cursor.FIELD_TYPE_FLOAT:
                                row.put(c.getDouble(i));
                                break;
                            case Cursor.FIELD_TYPE_BLOB:
                                byte[] blob = c.getBlob(i);
                                if (b64) {
                                    JSONObject wrapped = new JSONObject();
                                    wrapped.put("$b64", blob == null ? "" : Base64.encodeToString(blob, Base64.NO_WRAP));
                                    row.put(wrapped);
                                } else {
                                    row.put("<blob " + (blob == null ? 0 : blob.length) + " bytes>");
                                }
                                break;
                            default:
                                row.put(c.getString(i));
                        }
                    }
                    rows.put(row);
                    n++;
                }
                res.put("columns", columns);
                res.put("rows", rows);
                res.put("row_count", n);
                res.put("truncated", truncated);
                return res;
            } finally {
                c.close();
            }
        } finally {
            db.close();
        }
    }

    /**
     * Execute a write. With dryRun the statement runs inside a transaction that is never marked
     * successful, so endTransaction rolls it back — the caller still learns how many rows it would
     * have touched, which is the only safe way to check a WHERE clause against live app data.
     */
    private static JSONObject write(String path, String sql, boolean dryRun) throws Exception {
        SQLiteDatabase db = SQLiteDatabase.openDatabase(path, null, SQLiteDatabase.OPEN_READWRITE);
        try {
            JSONObject res = new JSONObject();
            db.beginTransaction();
            try {
                db.execSQL(sql);
                Cursor c = db.rawQuery("SELECT changes()", null);
                try {
                    if (c.moveToFirst()) {
                        res.put("changes", c.getLong(0));
                    }
                } finally {
                    c.close();
                }
                if (!dryRun) {
                    db.setTransactionSuccessful();
                }
            } finally {
                db.endTransaction();   // without setTransactionSuccessful this rolls back
            }
            res.put("ok", true);
            res.put("dry_run", dryRun);
            return res;
        } finally {
            db.close();
        }
    }

    private Main() {
    }
}
