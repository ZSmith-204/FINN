"""API-layer tests for FINN (stdlib ``unittest``, not pytest).

Covers README §5 (api parsing/params), §5.5 (statement kinds/HTTP semantics),
§6.2/§6.4 (response shaping + pagination), §7 (error contract), and
§8.1/§8.3 (generated ``API.py`` / ``main.py``).

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import ast
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from api import (  # noqa: E402
    API_INIT_CONFIG,
    API_INIT_FILE,
    API_INIT_UNKNOWN,
    METHODS,
    ApiSpec,
    ConfigError,
    Endpoint,
    Operation,
    Response,
    ValidationError,
    extract_params,
    generate,
    api_init,
    parse_api,
    render_api_module,
    render_main_module,
    split_statements,
    statement_kind,
)
from database import normalize_type  # noqa: E402

# The exact `api` block from README §12.
EXAMPLE_API = {
    "basePath": "/api",
    "endpoints": {
        "/users": {
            "get": "SELECT id, name, email FROM users",
            "post": {"sql": "INSERT INTO users (name, email) VALUES (:name, :email)"},
        },
        "/users/{id}": {
            "get": {
                "sql": "SELECT * FROM users WHERE id = :id",
                "response": {"shape": "single"},
            },
            "delete": "DELETE FROM users WHERE id = :id",
        },
        "/orders": {
            "get": {
                "sql": "SELECT * FROM orders WHERE user_id = :user_id",
                "params": {"user_id": {"source": "query", "required": False}},
                "response": {"paginate": True},
            }
        },
    },
}


def _api(**endpoints):
    return {"endpoints": endpoints}


class StatementKindTests(unittest.TestCase):
    """README §5.5 — statement classification."""

    def test_select(self):
        self.assertEqual(statement_kind("SELECT * FROM users"), "SELECT")
        self.assertEqual(statement_kind("  \n\tselect 1"), "SELECT")

    def test_leading_comments_are_skipped(self):
        self.assertEqual(
            statement_kind("-- generated\n/* note */ insert into t values (1)"),
            "INSERT",
        )

    def test_insert_update_delete(self):
        self.assertEqual(statement_kind("INSERT INTO t VALUES (1)"), "INSERT")
        self.assertEqual(statement_kind("UPDATE t SET a = 1"), "UPDATE")
        self.assertEqual(statement_kind("DELETE FROM t"), "DELETE")

    def test_with_is_select(self):
        self.assertEqual(
            statement_kind("WITH x AS (SELECT 1) SELECT * FROM x"), "SELECT"
        )

    def test_replace_is_insert(self):
        self.assertEqual(statement_kind("REPLACE INTO t VALUES (1)"), "INSERT")

    def test_ddl_is_other(self):
        for sql in (
            "CREATE TABLE t (id INTEGER)",
            "ALTER TABLE t ADD COLUMN x TEXT",
            "DROP TABLE t",
            "PRAGMA foreign_keys=ON",
            "VACUUM",
            "",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(statement_kind(sql), "OTHER")


class SplitStatementsTests(unittest.TestCase):
    """README §5.4 — splitting multi-statement SQL on top-level ``;``."""

    def test_splits_multiple_statements(self):
        statements = split_statements(
            "INSERT INTO t VALUES (1); UPDATE t SET a=2; SELECT * FROM t"
        )
        self.assertEqual(len(statements), 3)

    def test_semicolon_inside_quoted_string_does_not_split(self):
        statements = split_statements("INSERT INTO t (name) VALUES ('a;b')")
        self.assertEqual(len(statements), 1)


class ExtractParamsTests(unittest.TestCase):
    """README §5.3 — named bind parameter scanning."""

    def test_named_sigils(self):
        self.assertEqual(
            extract_params(
                "SELECT * FROM t WHERE a = :a AND b = @b AND c = $c"
            ),
            ["a", "b", "c"],
        )

    def test_deduped_in_order(self):
        self.assertEqual(extract_params("SELECT :b, :a, :b, :c, :a"), ["b", "a", "c"])

    def test_skips_single_quoted_strings(self):
        self.assertEqual(
            extract_params("SELECT ':notaparam' AS x WHERE id = :id"), ["id"]
        )

    def test_skips_double_quoted_identifiers(self):
        self.assertEqual(
            extract_params('SELECT "a:b" FROM t WHERE x = :x'), ["x"]
        )

    def test_skips_line_comments(self):
        self.assertEqual(
            extract_params("SELECT 1 -- :ignored\n WHERE x = :x"), ["x"]
        )

    def test_skips_block_comments(self):
        self.assertEqual(
            extract_params("SELECT /* :ignored */ 1 WHERE y = @y"), ["y"]
        )

    def test_bare_question_mark_raises(self):
        with self.assertRaises(ConfigError) as caught:
            extract_params("SELECT * FROM t WHERE id = ?")
        self.assertIn("positional", caught.exception.message)
        self.assertIn("?", caught.exception.message)

    def test_question_mark_in_string_does_not_raise(self):
        self.assertEqual(
            extract_params("SELECT '?' AS q WHERE id = :id"), ["id"]
        )

    def test_question_mark_in_comments_does_not_raise(self):
        self.assertEqual(
            extract_params("SELECT 'a' -- ?\n /* ? */ WHERE id = :id"), ["id"]
        )

    def test_no_params(self):
        self.assertEqual(extract_params("SELECT 1"), [])


class ParseApiExampleTests(unittest.TestCase):
    """README §5/§6.2/§12 — parsing the worked example."""

    def setUp(self):
        self.spec = parse_api(EXAMPLE_API)

    def test_base_path(self):
        self.assertIsInstance(self.spec, ApiSpec)
        self.assertEqual(self.spec.base_path, "/api")

    def test_endpoints_and_methods(self):
        self.assertEqual(
            set(self.spec.endpoints),
            {"/users", "/users/{id}", "/orders"},
        )
        self.assertEqual(set(self.spec.endpoints["/users"].operations), {"get", "post"})
        self.assertEqual(
            set(self.spec.endpoints["/users/{id}"].operations), {"get", "delete"}
        )
        self.assertEqual(set(self.spec.endpoints["/orders"].operations), {"get"})

    def test_operation_sql_and_shorthand(self):
        users = self.spec.endpoints["/users"].operations
        self.assertEqual(users["get"].sql, "SELECT id, name, email FROM users")
        self.assertEqual(
            users["post"].sql,
            "INSERT INTO users (name, email) VALUES (:name, :email)",
        )
        self.assertEqual(
            users["get"].method, "get"
        )
        self.assertEqual(statement_kind(users["post"].sql), "INSERT")

    def test_param_sources(self):
        users = self.spec.endpoints["/users"].operations["post"]
        self.assertEqual(users.params["name"].source, "body")
        self.assertEqual(users.params["email"].source, "body")

        item = self.spec.endpoints["/users/{id}"].operations["get"]
        self.assertEqual(item.params["id"].source, "path")
        self.assertTrue(item.params["id"].required)
        self.assertIsNone(item.params["id"].fin_type)

        delete = self.spec.endpoints["/users/{id}"].operations["delete"]
        self.assertEqual(delete.params["id"].source, "path")

        orders = self.spec.endpoints["/orders"].operations["get"]
        self.assertEqual(orders.params["user_id"].source, "query")
        self.assertFalse(orders.params["user_id"].required)

    def test_response_shapes(self):
        users_get = self.spec.endpoints["/users"].operations["get"]
        self.assertEqual(users_get.response.shape, "list")
        self.assertFalse(users_get.response.paginate)

        item_get = self.spec.endpoints["/users/{id}"].operations["get"]
        self.assertEqual(item_get.response.shape, "single")
        self.assertIsNone(item_get.response.key)

        orders_get = self.spec.endpoints["/orders"].operations["get"]
        self.assertEqual(orders_get.response.shape, "list")
        self.assertTrue(orders_get.response.paginate)

    def test_defaults(self):
        spec = parse_api(_api(**{"/x": {"get": "SELECT 1"}}))
        self.assertEqual(spec.base_path, "/")
        operation = spec.endpoints["/x"].operations["get"]
        self.assertFalse(operation.multi)
        self.assertIsNone(operation.description)
        self.assertIsNone(operation.authentication)
        self.assertIsNone(operation.rate_limit)
        self.assertEqual(operation.response.shape, "list")


class ParseApiValidationTests(unittest.TestCase):
    """README §5 — strict validation and path-scoped ConfigErrors."""

    def assert_config_error(self, expected_path, api, message=None):
        with self.assertRaises(ConfigError) as caught:
            parse_api(api)
        self.assertEqual(caught.exception.path, expected_path)
        if message is not None:
            self.assertIn(message, caught.exception.message)

    def test_unknown_method_key(self):
        self.assert_config_error(
            "api.endpoints./x.fetch",
            _api(**{"/x": {"fetch": "SELECT 1"}}),
            "unknown HTTP method",
        )

    def test_head_and_options_are_ignored(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "head": "SELECT 1",
                        "options": "SELECT 1",
                    }
                }
            )
        )
        self.assertEqual(spec.endpoints["/x"].operations, {})

    def test_methods_are_lowercase_normalized(self):
        spec = parse_api(_api(**{"/x": {"GET": "SELECT 1"}}))
        self.assertEqual(set(spec.endpoints["/x"].operations), {"get"})
        self.assertEqual(METHODS, ("get", "post", "put", "patch", "delete"))

    def test_unknown_operation_key(self):
        self.assert_config_error(
            "api.endpoints./x.get.bogus",
            _api(**{"/x": {"get": {"sql": "SELECT 1", "bogus": True}}}),
            "unknown key",
        )

    def test_missing_sql(self):
        self.assert_config_error(
            "api.endpoints./x.get.sql",
            _api(**{"/x": {"get": {"response": {"shape": "list"}}}}),
            "required",
        )

    def test_ddl_statement_is_config_error(self):
        self.assert_config_error(
            "api.endpoints./x.get.sql",
            _api(**{"/x": {"get": "CREATE TABLE t (id INTEGER)"}}),
            "unsupported SQL statement",
        )

    def test_multiple_statements_require_multi(self):
        self.assert_config_error(
            "api.endpoints./x.get.sql",
            _api(**{"/x": {"get": "SELECT 1; SELECT 2"}}),
            'multi": true',
        )

    def test_multi_true_allows_multiple_statements(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "post": {
                            "sql": "INSERT INTO t (a) VALUES (:a); "
                            "UPDATE t SET a = :a",
                            "multi": True,
                            "params": {"a": {"source": "body", "type": "integer"}},
                        }
                    }
                }
            )
        )
        operation = spec.endpoints["/x"].operations["post"]
        self.assertTrue(operation.multi)
        self.assertEqual(operation.params["a"].source, "body")

    def test_trailing_semicolon_is_one_statement(self):
        spec = parse_api(_api(**{"/x": {"get": "SELECT 1;"}}))
        self.assertFalse(spec.endpoints["/x"].operations["get"].multi)

    def test_paginate_requires_list_shape(self):
        self.assert_config_error(
            "api.endpoints./x.get.response.paginate",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t",
                            "response": {"shape": "single", "paginate": True},
                        }
                    }
                }
            ),
            'shape "list"',
        )

    def test_paginate_requires_select(self):
        self.assert_config_error(
            "api.endpoints./x.delete.response.paginate",
            _api(
                **{
                    "/x": {
                        "delete": {
                            "sql": "DELETE FROM t",
                            "response": {"paginate": True},
                        }
                    }
                }
            ),
            "SELECT",
        )

    def test_reserved_pagination_params(self):
        self.assert_config_error(
            "api.endpoints./x.get.sql",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t LIMIT :__pageSize",
                            "params": {"__pageSize": {"source": "query"}},
                            "response": {"paginate": True},
                        }
                    }
                }
            ),
            "reserved",
        )

    def test_pagination_reserves_page_names(self):
        self.assert_config_error(
            "api.endpoints./x.get.params.page",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE page = :page",
                            "params": {"page": {"source": "query"}},
                            "response": {"paginate": True},
                        }
                    }
                }
            ),
            "reserved",
        )

    def test_declared_header_param_is_allowed(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE req = :x_req",
                            "params": {
                                "x_req": {"source": "header", "required": True}
                            },
                        }
                    }
                }
            )
        )
        param = spec.endpoints["/x"].operations["get"].params["x_req"]
        self.assertEqual(param.source, "header")
        self.assertTrue(param.required)

    def test_undeclared_placeholder_defaults_by_method(self):
        # Header parameters must be declared; an undeclared placeholder is
        # resolved by method (GET/DELETE -> query, POST/PUT/PATCH -> body).
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": "SELECT * FROM t WHERE x_request_id = :x_request_id",
                        "post": "INSERT INTO t (x_request_id) VALUES (:x_request_id)",
                    }
                }
            )
        )
        operation = spec.endpoints["/x"].operations
        self.assertEqual(
            operation["get"].params["x_request_id"].source, "query"
        )
        self.assertEqual(
            operation["post"].params["x_request_id"].source, "body"
        )

    def test_path_parameter_is_implicit_and_cannot_be_declared(self):
        self.assert_config_error(
            "api.endpoints./x/{id}.get.params.id",
            _api(
                **{
                    "/x/{id}": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {"id": {"source": "query"}},
                        }
                    }
                }
            ),
            "implicit",
        )

    def test_unknown_param_source(self):
        self.assert_config_error(
            "api.endpoints./x.get.params.id.source",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {"id": {"source": "cookie"}},
                        }
                    }
                }
            ),
            "invalid source",
        )

    def test_path_source_requires_matching_segment(self):
        self.assert_config_error(
            "api.endpoints./x.get.params.id",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {"id": {"source": "path"}},
                        }
                    }
                }
            ),
            "does not match any",
        )

    def test_path_source_with_matching_segment_allows_type(self):
        spec = parse_api(
            _api(
                **{
                    "/x/{id}": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {
                                "id": {"source": "path", "type": "integer"}
                            },
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        self.assertIn("id: int", source)

    def test_unknown_param_type(self):
        self.assert_config_error(
            "api.endpoints./x.get.params.status.type",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE status = :status",
                            "params": {
                                "status": {"source": "query", "type": "varcharish"}
                            },
                        }
                    }
                }
            ),
            "unknown type",
        )

    def test_param_type_is_normalized(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {
                                "id": {"source": "query", "type": "int"}
                            },
                        }
                    }
                }
            )
        )
        param = spec.endpoints["/x"].operations["get"].params["id"]
        self.assertEqual(param.fin_type, "integer")
        self.assertEqual(param.fin_type, normalize_type("int"))

    def test_bare_question_mark_path_is_scoped(self):
        self.assert_config_error(
            "api.endpoints./x.get.sql",
            _api(**{"/x": {"get": "SELECT * FROM t WHERE id = ?"}}),
        )

    def test_positional_marker_in_string_is_fine(self):
        spec = parse_api(
            _api(**{"/x": {"get": "SELECT '?' AS q WHERE id = :id"}})
        )
        self.assertEqual(
            spec.endpoints["/x"].operations["get"].params["id"].source,
            "query",
        )

    def test_invalid_shape(self):
        self.assert_config_error(
            "api.endpoints./x.get.response.shape",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT 1",
                            "response": {"shape": "paged"},
                        }
                    }
                }
            ),
            "invalid shape",
        )

    def test_columns_and_renames(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT id, name FROM t",
                            "response": {
                                "columns": ["id", "name"],
                                "renames": {"id": "userId"},
                                "key": "ignored",
                            },
                        }
                    }
                }
            )
        )
        response = spec.endpoints["/x"].operations["get"].response
        self.assertEqual(response.columns, ["id", "name"])
        self.assertEqual(response.renames, {"id": "userId"})
        self.assertIsNone(response.key)

    def test_single_key(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT 1 AS one",
                            "response": {"shape": "single", "key": "row"},
                        }
                    }
                }
            )
        )
        response = spec.endpoints["/x"].operations["get"].response
        self.assertEqual(response.shape, "single")
        self.assertEqual(response.key, "row")

    def test_blob_format_is_parsed(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT data FROM t",
                            "response": {"blob": "$b64:{}"},
                        }
                    }
                }
            )
        )
        self.assertEqual(
            spec.endpoints["/x"].operations["get"].response.blob_format,
            "$b64:{}",
        )

    def test_reserved_auth_and_rate_limit_are_stored(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT 1",
                            "description": "probe",
                            "authentication": "none",
                            "rateLimit": {"rpm": 10},
                        }
                    }
                }
            )
        )
        operation = spec.endpoints["/x"].operations["get"]
        self.assertEqual(operation.description, "probe")
        self.assertEqual(operation.authentication, "none")
        self.assertEqual(operation.rate_limit, {"rpm": 10})

    def test_unknown_api_key(self):
        self.assert_config_error(
            "api.bogus",
            {"basePath": "/", "endpoints": {}, "bogus": True},
            "unknown key",
        )

    def test_cors_key_accepted(self):
        spec = parse_api({"endpoints": {}, "cors": {"origins": ["*"]}})
        self.assertEqual(spec.cors, {"origins": ["*"]})

    def test_missing_endpoints(self):
        self.assert_config_error("api.endpoints", {"basePath": "/"}, "required")

    def test_paths_are_normalized(self):
        spec = parse_api(
            {"endpoints": {"users": {"get": "SELECT 1"}}, "basePath": "v1"}
        )
        self.assertEqual(spec.base_path, "/v1")
        self.assertEqual(set(spec.endpoints), {"/users"})

        trimmed = parse_api(
            {"endpoints": {"users": {"get": "SELECT 1"}}, "basePath": "/v1/"}
        )
        self.assertEqual(trimmed.base_path, "/v1")

    def test_pagination_cannot_be_multi_statement(self):
        self.assert_config_error(
            "api.endpoints./x.get.response.paginate",
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT 1; SELECT 2",
                            "multi": True,
                            "response": {"paginate": True},
                        }
                    }
                }
            ),
            "pagination",
        )

    def test_duplicate_endpoint_path_after_normalization(self):
        self.assert_config_error(
            "api.endpoints./users",
            {"endpoints": {"/users": {"get": "SELECT 1"}, "users": {"get": "SELECT 1"}}},
            "duplicate endpoint",
        )


class RenderApiTests(unittest.TestCase):
    """README §7/§8.3 — generated ``API.py``."""

    def setUp(self):
        self.spec = parse_api(EXAMPLE_API)
        self.source = render_api_module(self.spec)

    def test_ast_parses(self):
        ast.parse(self.source)
        compile(self.source, "API.py", "exec")

    def test_router_and_decorators(self):
        self.assertIn("router = APIRouter()", self.source)
        self.assertIn("@router.get('/users')", self.source)
        self.assertIn("@router.post('/users')", self.source)
        self.assertIn("@router.get('/users/{id}')", self.source)
        self.assertIn("@router.delete('/users/{id}')", self.source)
        self.assertIn("@router.get('/orders')", self.source)

    def test_imports_db_layer(self):
        self.assertIn("from DB import", self.source)
        for symbol in ("query", "coerce", "map_sqlite_error", "DB_PATH"):
            self.assertIn(symbol, self.source)

    def test_sql_strings_are_embedded(self):
        for operation in (
            "SELECT id, name, email FROM users",
            "INSERT INTO users (name, email) VALUES (:name, :email)",
            "SELECT * FROM users WHERE id = :id",
            "DELETE FROM users WHERE id = :id",
        ):
            self.assertIn(repr(operation), self.source)

    def test_error_contract(self):
        self.assertIn("def _fail(", self.source)
        self.assertIn('{"error": {"code": code, "message": message, "detail": detail}}', self.source)
        self.assertIn('"VALIDATION_ERROR"', self.source)
        self.assertIn('"NOT_FOUND"', self.source)
        self.assertIn('"INTERNAL_ERROR"', self.source)

    def test_query_parameter_signature(self):
        self.assertIn("user_id: Optional[str] = None", self.source)

    def test_pagination_codegen(self):
        self.assertIn("page: int = Query(1, ge=1)", self.source)
        self.assertIn("pageSize: int = Query(50, ge=1, le=1000)", self.source)
        self.assertIn("SELECT COUNT(*) AS __c FROM (", self.source)
        self.assertIn("LIMIT :__pageSize OFFSET :__offset", self.source)
        self.assertIn('"items": rows', self.source)
        self.assertIn('"totalPages": math.ceil(total / pageSize)', self.source)

    def test_mutation_semantics(self):
        self.assertIn('return {"affected": result["affected"], "rowid": result["rowid"]}', self.source)
        self.assertIn("affected == 0", self.source)
        self.assertIn('return _fail(404, "NOT_FOUND"', self.source)

    def test_post_select_empty_rows_returns_404(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "post": {
                            "sql": "SELECT * FROM t WHERE id = :id",
                            "params": {
                                "id": {"source": "body", "type": "integer"}
                            },
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("if not rows:", source)
        self.assertIn('"NOT_FOUND"', source)

    def test_shape_none_and_single_key(self):
        spec = parse_api(
            _api(
                **{
                    "/a": {"get": {"sql": "SELECT 1", "response": {"shape": "none"}}},
                    "/b": {
                        "get": {
                            "sql": "SELECT 1 AS one",
                            "response": {"shape": "single", "key": "row"},
                        }
                    },
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("return Response(status_code=204)", source)
        self.assertIn("return {'row': rows[0]}", source)

    def test_typed_and_required_params(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "post": {
                            "sql": "INSERT INTO t (n, flag, ratio, req) "
                            "VALUES (:n, :flag, :ratio, :req)",
                            "params": {
                                "n": {"source": "body", "type": "integer"},
                                "flag": {"source": "body", "type": "boolean"},
                                "ratio": {"source": "body", "type": "real"},
                                "req": {"source": "body", "required": True},
                            },
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("payload: Dict[str, Any] = Body(None)", source)
        self.assertIn("_coerce('n', 'integer', payload['n'])", source)
        self.assertIn("_coerce('flag', 'boolean', payload['flag'])", source)
        self.assertIn("_coerce('ratio', 'real', payload['ratio'])", source)
        self.assertIn("Missing required body field req.", source)

    def test_header_alias_and_required_check(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t WHERE req = :x_req",
                            "params": {
                                "x_req": {"source": "header", "required": True}
                            },
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("Header(None, alias='x-req')", source)
        self.assertIn("Missing required header parameter x_req.", source)

    def test_query_default(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "get": {
                            "sql": "SELECT * FROM t LIMIT :limit",
                            "params": {
                                "limit": {
                                    "source": "query",
                                    "type": "integer",
                                    "default": 50,
                                }
                            },
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("limit: Optional[int] = Query(50)", source)

    def test_multi_statement_codegen(self):
        spec = parse_api(
            _api(
                **{
                    "/x": {
                        "post": {
                            "sql": "INSERT INTO t (a) VALUES (:a); UPDATE t SET a = :a",
                            "multi": True,
                            "params": {"a": {"source": "body", "type": "integer"}},
                        }
                    }
                }
            )
        )
        source = render_api_module(spec)
        ast.parse(source)
        self.assertIn("for _s in [", source)
        self.assertIn("conn.commit()", source)
        self.assertIn("conn.rollback()", source)
        self.assertIn("serialize_rows", source)


class RenderMainTests(unittest.TestCase):
    """README §8.1 — generated ``main.py``."""

    def test_ast_parses_and_mounts_router(self):
        spec = parse_api(EXAMPLE_API)
        source = render_main_module(spec)
        ast.parse(source)
        compile(source, "main.py", "exec")
        self.assertIn("app = FastAPI()", source)
        self.assertIn("from API import router", source)
        self.assertIn("app.include_router(router, prefix='/api')", source)
        self.assertIn('@app.get("/healthz")', source)
        self.assertIn('return {"status": "ok"}', source)
        self.assertIn('__all__ = ["app"]', source)

    def test_default_prefix(self):
        spec = parse_api(_api(**{"/x": {"get": "SELECT 1"}}))
        source = render_main_module(spec)
        ast.parse(source)
        self.assertIn("app.include_router(router)", source)
        self.assertNotIn("prefix=", source)


class GenerateTests(unittest.TestCase):
    """README §8.1/§8.3 — build emission + safe wrapper."""

    def setUp(self):
        self.spec = parse_api(EXAMPLE_API)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_dir = os.path.join(self.tmpdir.name, "build")

    def test_generate_writes_both_modules(self):
        paths = generate(self.output_dir, self.spec)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(os.path.isabs(path) for path in paths))
        self.assertEqual(
            {os.path.basename(path) for path in paths},
            {"API.py", "main.py"},
        )
        for path in paths:
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            ast.parse(source, filename=path)

    def test_generate_manual_invalid_spec_raises_config_error(self):
        bad_spec = ApiSpec(
            base_path="/",
            endpoints={
                "/x": Endpoint(
                    path="/x",
                    operations={
                        "get": Operation(method="get", sql="DROP TABLE users")
                    },
                )
            },
        )
        with self.assertRaises(ConfigError) as caught:
            generate(self.output_dir, bad_spec)
        self.assertEqual(caught.exception.path, "api.endpoints./x.get.sql")

    def test_api_init_success(self):
        self.assertEqual(api_init(self.output_dir, self.spec), (True, None))
        self.assertTrue(os.path.isfile(os.path.join(self.output_dir, "API.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.output_dir, "main.py")))

    def test_api_init_file_error(self):
        file_path = os.path.join(self.tmpdir.name, "not-a-dir")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertEqual(api_init(file_path, self.spec), (False, API_INIT_FILE))
        self.assertEqual(API_INIT_FILE, "FILE_ERROR")

    def test_api_init_config_error(self):
        bad_spec = ApiSpec(
            base_path="/",
            endpoints={
                "/x": Endpoint(
                    path="/x",
                    operations={
                        "get": Operation(
                            method="get", sql="DROP TABLE users"
                        )
                    },
                )
            },
        )
        self.assertEqual(
            api_init(self.output_dir, bad_spec), (False, API_INIT_CONFIG)
        )
        self.assertEqual(API_INIT_CONFIG, "CONFIG_ERROR")
        self.assertEqual(API_INIT_UNKNOWN, "UNKNOWN")

    def test_validation_error_is_the_database_one(self):
        # The generated layer raises the same ValidationError class DB.py
        # exports; coerced 400 behavior is exercised at app level only.
        self.assertTrue(issubclass(ValidationError, ValueError))


if __name__ == "__main__":
    unittest.main()