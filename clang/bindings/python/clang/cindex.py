# ===- cindex.py - Python Indexing Library Bindings -----------*- python -*--===#
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ===------------------------------------------------------------------------===#

r"""
Clang Indexing Library Bindings
===============================

This module provides an interface to the Clang indexing library. It is a
low-level interface to the indexing library which attempts to match the Clang
API directly while also being "pythonic". Notable differences from the C API
are:

 * string results are returned as Python strings, not CXString objects.

 * null cursors are translated to None.

 * access to child cursors is done via iteration, not visitation.

The major indexing objects are:

  Index

    The top-level object which manages some global library state.

  TranslationUnit

    High-level object encapsulating the AST for a single translation unit. These
    can be loaded from .ast files or parsed on the fly.

  Cursor

    Generic object for representing a node in the AST.

  SourceRange, SourceLocation, and File

    Objects representing information about the input source.

Most object information is exposed using properties, when the underlying API
call is efficient.
"""
from __future__ import annotations

# TODO
# ====
#
# o API support for invalid translation units. Currently we can't even get the
#   diagnostics on failure because they refer to locations in an object that
#   will have been invalidated.
#
# o fix memory management issues (currently client must hold on to index and
#   translation unit, or risk crashes).
#
# o expose code completion APIs.
#
# o cleanup ctypes wrapping, would be nice to separate the ctypes details more
#   clearly, and hide from the external interface (i.e., help(cindex)).
#
# o implement additional SourceLocation, SourceRange, and File methods.

from ctypes import (
    Array,
    CDLL,
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int,
    c_longlong,
    c_uint,
    c_ulong,
    c_ulonglong,
    c_void_p,
    cast,
    cdll,
    py_object,
)

import os
import sys
from enum import Enum

from typing import (
    Any,
    Callable,
    cast as Tcast,
    Generic,
    Optional,
    Sequence,
    Type as TType,
    TypeVar,
    TYPE_CHECKING,
    Union as TUnion,
)

if TYPE_CHECKING:
    from ctypes import _Pointer
    from typing_extensions import Protocol, TypeAlias

    StrPath: TypeAlias = TUnion[str, os.PathLike[str]]
    LibFunc: TypeAlias = TUnion[
        "tuple[str, Optional[list[Any]]]",
        "tuple[str, Optional[list[Any]], Any]",
        "tuple[str, Optional[list[Any]], Any, Callable[..., Any]]",
    ]

    TSeq = TypeVar("TSeq", covariant=True)

    class NoSliceSequence(Protocol[TSeq]):
        def __len__(self) -> int:
            ...

        def __getitem__(self, key: int) -> TSeq:
            ...


# Python 3 strings are unicode, translate them to/from utf8 for C-interop.
class c_interop_string(c_char_p):
    def __init__(self, p: str | bytes | None = None):
        if p is None:
            p = ""
        if isinstance(p, str):
            p = p.encode("utf8")
        super(c_char_p, self).__init__(p)

    def __str__(self) -> str:
        return self.value or ""

    @property
    def value(self) -> str | None:  # type: ignore [override]
        val = super(c_char_p, self).value
        if val is None:
            return None
        return val.decode("utf8")

    @classmethod
    def from_param(cls, param: str | bytes | None) -> c_interop_string:
        if isinstance(param, str):
            return cls(param)
        if isinstance(param, bytes):
            return cls(param)
        if param is None:
            # Support passing null to C functions expecting char arrays
            return cls(param)
        raise TypeError(
            "Cannot convert '{}' to '{}'".format(type(param).__name__, cls.__name__)
        )

    @staticmethod
    def to_python_string(x: c_interop_string) -> str | None:
        return x.value


def b(x: str | bytes) -> bytes:
    if isinstance(x, bytes):
        return x
    return x.encode("utf8")


# ctypes doesn't implicitly convert c_void_p to the appropriate wrapper
# object. This is a problem, because it means that from_parameter will see an
# integer and pass the wrong value on platforms where int != void*. Work around
# this by marshalling object arguments as void**.
c_object_p: TType[_Pointer[Any]] = POINTER(c_void_p)

### Exception Classes ###


class TranslationUnitLoadError(Exception):
    """Represents an error that occurred when loading a TranslationUnit.

    This is raised in the case where a TranslationUnit could not be
    instantiated due to failure in the libclang library.

    FIXME: Make libclang expose additional error information in this scenario.
    """

    pass


class TranslationUnitSaveError(Exception):
    """Represents an error that occurred when saving a TranslationUnit.

    Each error has associated with it an enumerated value, accessible under
    e.save_error. Consumers can compare the value with one of the ERROR_
    constants in this class.
    """

    # Indicates that an unknown error occurred. This typically indicates that
    # I/O failed during save.
    ERROR_UNKNOWN = 1

    # Indicates that errors during translation prevented saving. The errors
    # should be available via the TranslationUnit's diagnostics.
    ERROR_TRANSLATION_ERRORS = 2

    # Indicates that the translation unit was somehow invalid.
    ERROR_INVALID_TU = 3

    def __init__(self, enumeration, message):
        assert isinstance(enumeration, int)

        if enumeration < 1 or enumeration > 3:
            raise Exception(
                "Encountered undefined TranslationUnit save error "
                "constant: %d. Please file a bug to have this "
                "value supported." % enumeration
            )

        self.save_error = enumeration
        Exception.__init__(self, "Error %d: %s" % (enumeration, message))


### Structures and Utility Classes ###

TInstance = TypeVar("TInstance")
TResult = TypeVar("TResult")


class CachedProperty(Generic[TInstance, TResult]):
    """Decorator that lazy-loads the value of a property.

    The first time the property is accessed, the original property function is
    executed. The value it returns is set as the new value of that instance's
    property, replacing the original method.
    """

    def __init__(self, wrapped: Callable[[TInstance], TResult]):
        self.wrapped = wrapped
        try:
            self.__doc__ = wrapped.__doc__
        except:
            pass

    def __get__(self, instance: TInstance, instance_type: Any = None) -> TResult:
        if instance is None:
            property_name = self.wrapped.__name__
            class_name = instance_type.__name__
            raise TypeError(
                f"'{property_name}' is not a static attribute of '{class_name}'"
            )

        value = self.wrapped(instance)
        setattr(instance, self.wrapped.__name__, value)

        return value


class _CXString(Structure):
    """Helper for transforming CXString results."""

    _fields_ = [("spelling", c_char_p), ("free", c_int)]

    def __del__(self) -> None:
        conf.lib.clang_disposeString(self)

    @staticmethod
    def from_result(res: _CXString) -> str:
        assert isinstance(res, _CXString)
        pystr = c_interop_string.to_python_string(conf.lib.clang_getCString(res))
        if pystr is None:
            return ""
        return pystr


class SourceLocation(Structure):
    """
    A SourceLocation represents a particular location within a source file.
    """

    _fields_ = [("ptr_data", c_void_p * 2), ("int_data", c_uint)]
    _data = None

    def _get_instantiation(self):
        if self._data is None:
            f, l, c, o = c_object_p(), c_uint(), c_uint(), c_uint()
            conf.lib.clang_getInstantiationLocation(
                self, byref(f), byref(l), byref(c), byref(o)
            )
            if f:
                f = File(f)
            else:
                f = None
            self._data = (f, int(l.value), int(c.value), int(o.value))
        return self._data

    @staticmethod
    def from_position(tu, file, line, column):
        """
        Retrieve the source location associated with a given file/line/column in
        a particular translation unit.
        """
        return conf.lib.clang_getLocation(tu, file, line, column)  # type: ignore [no-any-return]

    @staticmethod
    def from_offset(tu, file, offset):
        """Retrieve a SourceLocation from a given character offset.

        tu -- TranslationUnit file belongs to
        file -- File instance to obtain offset from
        offset -- Integer character offset within file
        """
        return conf.lib.clang_getLocationForOffset(tu, file, offset)  # type: ignore [no-any-return]

    @property
    def file(self):
        """Get the file represented by this source location."""
        return self._get_instantiation()[0]

    @property
    def line(self):
        """Get the line represented by this source location."""
        return self._get_instantiation()[1]

    @property
    def column(self):
        """Get the column represented by this source location."""
        return self._get_instantiation()[2]

    @property
    def offset(self):
        """Get the file offset represented by this source location."""
        return self._get_instantiation()[3]

    @property
    def is_in_system_header(self):
        """Returns true if the given source location is in a system header."""
        return conf.lib.clang_Location_isInSystemHeader(self)  # type: ignore [no-any-return]

    def __eq__(self, other):
        if not isinstance(other, SourceLocation):
            return False
        return conf.lib.clang_equalLocations(self, other)  # type: ignore [no-any-return]

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other: SourceLocation) -> bool:
        return conf.lib.clang_isBeforeInTranslationUnit(self, other)  # type: ignore [no-any-return]

    def __le__(self, other: SourceLocation) -> bool:
        return self < other or self == other

    def __repr__(self):
        if self.file:
            filename = self.file.name
        else:
            filename = None
        return "<SourceLocation file %r, line %r, column %r>" % (
            filename,
            self.line,
            self.column,
        )


class SourceRange(Structure):
    """
    A SourceRange describes a range of source locations within the source
    code.
    """

    _fields_ = [
        ("ptr_data", c_void_p * 2),
        ("begin_int_data", c_uint),
        ("end_int_data", c_uint),
    ]

    # FIXME: Eliminate this and make normal constructor? Requires hiding ctypes
    # object.
    @staticmethod
    def from_locations(start, end):
        return conf.lib.clang_getRange(start, end)  # type: ignore [no-any-return]

    @property
    def start(self):
        """
        Return a SourceLocation representing the first character within a
        source range.
        """
        return conf.lib.clang_getRangeStart(self)  # type: ignore [no-any-return]

    @property
    def end(self):
        """
        Return a SourceLocation representing the last character within a
        source range.
        """
        return conf.lib.clang_getRangeEnd(self)  # type: ignore [no-any-return]

    def __eq__(self, other):
        if not isinstance(other, SourceRange):
            return False
        return conf.lib.clang_equalRanges(self, other)  # type: ignore [no-any-return]

    def __ne__(self, other):
        return not self.__eq__(other)

    def __contains__(self, other):
        """Useful to detect the Token/Lexer bug"""
        if not isinstance(other, SourceLocation):
            return False
        return self.start <= other <= self.end

    def __repr__(self):
        return "<SourceRange start %r, end %r>" % (self.start, self.end)


class Diagnostic:
    """
    A Diagnostic is a single instance of a Clang diagnostic. It includes the
    diagnostic severity, the message, the location the diagnostic occurred, as
    well as additional source ranges and associated fix-it hints.
    """

    Ignored = 0
    Note = 1
    Warning = 2
    Error = 3
    Fatal = 4

    DisplaySourceLocation = 0x01
    DisplayColumn = 0x02
    DisplaySourceRanges = 0x04
    DisplayOption = 0x08
    DisplayCategoryId = 0x10
    DisplayCategoryName = 0x20
    _FormatOptionsMask = 0x3F

    def __init__(self, ptr):
        self.ptr = ptr

    def __del__(self):
        conf.lib.clang_disposeDiagnostic(self)

    @property
    def severity(self):
        return conf.lib.clang_getDiagnosticSeverity(self)  # type: ignore [no-any-return]

    @property
    def location(self):
        return conf.lib.clang_getDiagnosticLocation(self)  # type: ignore [no-any-return]

    @property
    def spelling(self):
        return _CXString.from_result(conf.lib.clang_getDiagnosticSpelling(self))

    @property
    def ranges(self) -> NoSliceSequence[SourceRange]:
        class RangeIterator:
            def __init__(self, diag: Diagnostic):
                self.diag = diag

            def __len__(self) -> int:
                return int(conf.lib.clang_getDiagnosticNumRanges(self.diag))

            def __getitem__(self, key: int) -> SourceRange:
                if key >= len(self):
                    raise IndexError
                return conf.lib.clang_getDiagnosticRange(self.diag, key)  # type: ignore [no-any-return]

        return RangeIterator(self)

    @property
    def fixits(self) -> NoSliceSequence[FixIt]:
        class FixItIterator:
            def __init__(self, diag: Diagnostic):
                self.diag = diag

            def __len__(self) -> int:
                return int(conf.lib.clang_getDiagnosticNumFixIts(self.diag))

            def __getitem__(self, key: int) -> FixIt:
                range = SourceRange()
                value = _CXString.from_result(
                    conf.lib.clang_getDiagnosticFixIt(self.diag, key, byref(range))
                )
                if len(value) == 0:
                    raise IndexError

                return FixIt(range, value)

        return FixItIterator(self)

    @property
    def children(self) -> NoSliceSequence[Diagnostic]:
        class ChildDiagnosticsIterator:
            def __init__(self, diag: Diagnostic):
                self.diag_set = conf.lib.clang_getChildDiagnostics(diag)

            def __len__(self) -> int:
                return int(conf.lib.clang_getNumDiagnosticsInSet(self.diag_set))

            def __getitem__(self, key: int) -> Diagnostic:
                diag = conf.lib.clang_getDiagnosticInSet(self.diag_set, key)
                if not diag:
                    raise IndexError
                return Diagnostic(diag)

        return ChildDiagnosticsIterator(self)

    @property
    def category_number(self):
        """The category number for this diagnostic or 0 if unavailable."""
        return conf.lib.clang_getDiagnosticCategory(self)  # type: ignore [no-any-return]

    @property
    def category_name(self):
        """The string name of the category for this diagnostic."""
        return _CXString.from_result(conf.lib.clang_getDiagnosticCategoryText(self))

    @property
    def option(self):
        """The command-line option that enables this diagnostic."""
        return _CXString.from_result(conf.lib.clang_getDiagnosticOption(self, None))

    @property
    def disable_option(self):
        """The command-line option that disables this diagnostic."""
        disable = _CXString()
        conf.lib.clang_getDiagnosticOption(self, byref(disable))
        return _CXString.from_result(disable)

    def format(self, options=None):
        """
        Format this diagnostic for display. The options argument takes
        Diagnostic.Display* flags, which can be combined using bitwise OR. If
        the options argument is not provided, the default display options will
        be used.
        """
        if options is None:
            options = conf.lib.clang_defaultDiagnosticDisplayOptions()
        if options & ~Diagnostic._FormatOptionsMask:
            raise ValueError("Invalid format options")
        return _CXString.from_result(conf.lib.clang_formatDiagnostic(self, options))

    def __repr__(self):
        return "<Diagnostic severity %r, location %r, spelling %r>" % (
            self.severity,
            self.location,
            self.spelling,
        )

    def __str__(self):
        return self.format()

    def from_param(self):
        return self.ptr


class FixIt:
    """
    A FixIt represents a transformation to be applied to the source to
    "fix-it". The fix-it should be applied by replacing the given source range
    with the given value.
    """

    def __init__(self, range, value):
        self.range = range
        self.value = value

    def __repr__(self):
        return "<FixIt range %r, value %r>" % (self.range, self.value)


class TokenGroup:
    """Helper class to facilitate token management.

    Tokens are allocated from libclang in chunks. They must be disposed of as a
    collective group.

    One purpose of this class is for instances to represent groups of allocated
    tokens. Each token in a group contains a reference back to an instance of
    this class. When all tokens from a group are garbage collected, it allows
    this class to be garbage collected. When this class is garbage collected,
    it calls the libclang destructor which invalidates all tokens in the group.

    You should not instantiate this class outside of this module.
    """

    def __init__(self, tu, memory, count):
        self._tu = tu
        self._memory = memory
        self._count = count

    def __del__(self):
        conf.lib.clang_disposeTokens(self._tu, self._memory, self._count)

    @staticmethod
    def get_tokens(tu, extent):
        """Helper method to return all tokens in an extent.

        This functionality is needed multiple places in this module. We define
        it here because it seems like a logical place.
        """
        tokens_memory = POINTER(Token)()
        tokens_count = c_uint()

        conf.lib.clang_tokenize(tu, extent, byref(tokens_memory), byref(tokens_count))

        count = int(tokens_count.value)

        # If we get no tokens, no memory was allocated. Be sure not to return
        # anything and potentially call a destructor on nothing.
        if count < 1:
            return

        tokens_array = cast(tokens_memory, POINTER(Token * count)).contents

        token_group = TokenGroup(tu, tokens_memory, tokens_count)

        for i in range(0, count):
            token = Token()
            token.int_data = tokens_array[i].int_data
            token.ptr_data = tokens_array[i].ptr_data
            token._tu = tu
            token._group = token_group

            yield token


### Cursor Kinds ###
class BaseEnumeration(Enum):
    """
    Common base class for named enumerations held in sync with Index.h values.
    """


    def from_param(self):
        return self.value

    @classmethod
    def from_id(cls, id):
        return cls(id)

    def __repr__(self):
        return "%s.%s" % (
            self.__class__.__name__,
            self.name,
        )


class TokenKind(BaseEnumeration):
    """Describes a specific type of a Token."""

    @classmethod
    def from_value(cls, value):
        """Obtain a registered TokenKind instance from its value."""
        return cls.from_id(value)

    PUNCTUATION = 0
    KEYWORD = 1
    IDENTIFIER = 2
    LITERAL = 3
    COMMENT = 4


class CursorKind(BaseEnumeration):
    """
    A CursorKind describes the kind of entity that a cursor points to.
    """

    @staticmethod
    def get_all_kinds():
        """Return all CursorKind enumeration instances."""
        return list(CursorKind)

    def is_declaration(self):
        """Test if this is a declaration kind."""
        return conf.lib.clang_isDeclaration(self)  # type: ignore [no-any-return]

    def is_reference(self):
        """Test if this is a reference kind."""
        return conf.lib.clang_isReference(self)  # type: ignore [no-any-return]

    def is_expression(self):
        """Test if this is an expression kind."""
        return conf.lib.clang_isExpression(self)  # type: ignore [no-any-return]

    def is_statement(self):
        """Test if this is a statement kind."""
        return conf.lib.clang_isStatement(self)  # type: ignore [no-any-return]

    def is_attribute(self):
        """Test if this is an attribute kind."""
        return conf.lib.clang_isAttribute(self)  # type: ignore [no-any-return]

    def is_invalid(self):
        """Test if this is an invalid kind."""
        return conf.lib.clang_isInvalid(self)  # type: ignore [no-any-return]

    def is_translation_unit(self):
        """Test if this is a translation unit kind."""
        return conf.lib.clang_isTranslationUnit(self)  # type: ignore [no-any-return]

    def is_preprocessing(self):
        """Test if this is a preprocessing kind."""
        return conf.lib.clang_isPreprocessing(self)  # type: ignore [no-any-return]

    def is_unexposed(self):
        """Test if this is an unexposed kind."""
        return conf.lib.clang_isUnexposed(self)  # type: ignore [no-any-return]


    ###
    # Declaration Kinds

    # A declaration whose specific kind is not exposed via this interface.
    #
    # Unexposed declarations have the same operations as any other kind of
    # declaration; one can extract their location information, spelling, find
    # their definitions, etc. However, the specific kind of the declaration is
    # not reported.
    UNEXPOSED_DECL = 1

    # A C or C++ struct.
    STRUCT_DECL = 2

    # A C or C++ union.
    UNION_DECL = 3

    # A C++ class.
    CLASS_DECL = 4

    # An enumeration.
    ENUM_DECL = 5

    # A field (in C) or non-static data member (in C++) in a struct, union, or
    # C++ class.
    FIELD_DECL = 6

    # An enumerator constant.
    ENUM_CONSTANT_DECL = 7

    # A function.
    FUNCTION_DECL = 8

    # A variable.
    VAR_DECL = 9

    # A function or method parameter.
    PARM_DECL = 10

    # An Objective-C @interface.
    OBJC_INTERFACE_DECL = 11

    # An Objective-C @interface for a category.
    OBJC_CATEGORY_DECL = 12

    # An Objective-C @protocol declaration.
    OBJC_PROTOCOL_DECL = 13

    # An Objective-C @property declaration.
    OBJC_PROPERTY_DECL = 14

    # An Objective-C instance variable.
    OBJC_IVAR_DECL = 15

    # An Objective-C instance method.
    OBJC_INSTANCE_METHOD_DECL = 16

    # An Objective-C class method.
    OBJC_CLASS_METHOD_DECL = 17

    # An Objective-C @implementation.
    OBJC_IMPLEMENTATION_DECL = 18

    # An Objective-C @implementation for a category.
    OBJC_CATEGORY_IMPL_DECL = 19

    # A typedef.
    TYPEDEF_DECL = 20

    # A C++ class method.
    CXX_METHOD = 21

    # A C++ namespace.
    NAMESPACE = 22

    # A linkage specification, e.g. 'extern "C"'.
    LINKAGE_SPEC = 23

    # A C++ constructor.
    CONSTRUCTOR = 24

    # A C++ destructor.
    DESTRUCTOR = 25

    # A C++ conversion function.
    CONVERSION_FUNCTION = 26

    # A C++ template type parameter
    TEMPLATE_TYPE_PARAMETER = 27

    # A C++ non-type template parameter.
    TEMPLATE_NON_TYPE_PARAMETER = 28

    # A C++ template template parameter.
    TEMPLATE_TEMPLATE_PARAMETER = 29

    # A C++ function template.
    FUNCTION_TEMPLATE = 30

    # A C++ class template.
    CLASS_TEMPLATE = 31

    # A C++ class template partial specialization.
    CLASS_TEMPLATE_PARTIAL_SPECIALIZATION = 32

    # A C++ namespace alias declaration.
    NAMESPACE_ALIAS = 33

    # A C++ using directive
    USING_DIRECTIVE = 34

    # A C++ using declaration
    USING_DECLARATION = 35

    # A Type alias decl.
    TYPE_ALIAS_DECL = 36

    # A Objective-C synthesize decl
    OBJC_SYNTHESIZE_DECL = 37

    # A Objective-C dynamic decl
    OBJC_DYNAMIC_DECL = 38

    # A C++ access specifier decl.
    CXX_ACCESS_SPEC_DECL = 39


    ###
    # Reference Kinds

    OBJC_SUPER_CLASS_REF = 40
    OBJC_PROTOCOL_REF = 41
    OBJC_CLASS_REF = 42

    # A reference to a type declaration.
    #
    # A type reference occurs anywhere where a type is named but not
    # declared. For example, given:
    #   typedef unsigned size_type;
    #   size_type size;
    #
    # The typedef is a declaration of size_type (CXCursor_TypedefDecl),
    # while the type of the variable "size" is referenced. The cursor
    # referenced by the type of size is the typedef for size_type.
    TYPE_REF = 43
    CXX_BASE_SPECIFIER = 44

    # A reference to a class template, function template, template
    # template parameter, or class template partial specialization.
    TEMPLATE_REF = 45

    # A reference to a namespace or namepsace alias.
    NAMESPACE_REF = 46

    # A reference to a member of a struct, union, or class that occurs in
    # some non-expression context, e.g., a designated initializer.
    MEMBER_REF = 47

    # A reference to a labeled statement.
    LABEL_REF = 48

    # A reference to a set of overloaded functions or function templates that
    # has not yet been resolved to a specific function or function template.
    OVERLOADED_DECL_REF = 49

    # A reference to a variable that occurs in some non-expression
    # context, e.g., a C++ lambda capture list.
    VARIABLE_REF = 50

    ###
    # Invalid/Error Kinds

    INVALID_FILE = 70
    NO_DECL_FOUND = 71
    NOT_IMPLEMENTED = 72
    INVALID_CODE = 73

    ###
    # Expression Kinds

    # An expression whose specific kind is not exposed via this interface.
    #
    # Unexposed expressions have the same operations as any other kind of
    # expression; one can extract their location information, spelling,
    # children, etc.
    # However, the specific kind of the expression is not reported.
    UNEXPOSED_EXPR = 100

    # An expression that refers to some value declaration, such as a function,
    # variable, or enumerator.
    DECL_REF_EXPR = 101

    # An expression that refers to a member of a struct, union, class,
    # Objective-C class, etc.
    MEMBER_REF_EXPR = 102

    # An expression that calls a function.
    CALL_EXPR = 103

    # An expression that sends a message to an Objective-C object or class.
    OBJC_MESSAGE_EXPR = 104

    # An expression that represents a block literal.
    BLOCK_EXPR = 105

    # An integer literal.
    INTEGER_LITERAL = 106

    # A floating point number literal.
    FLOATING_LITERAL = 107

    # An imaginary number literal.
    IMAGINARY_LITERAL = 108

    # A string literal.
    STRING_LITERAL = 109

    # A character literal.
    CHARACTER_LITERAL = 110

    # A parenthesized expression, e.g. "(1)".
    #
    # This AST node is only formed if full location information is requested.
    PAREN_EXPR = 111

    # This represents the unary-expression's (except sizeof and
    # alignof).
    UNARY_OPERATOR = 112

    # [C99 6.5.2.1] Array Subscripting.
    ARRAY_SUBSCRIPT_EXPR = 113

    # A builtin binary operation expression such as "x + y" or "x <= y".
    BINARY_OPERATOR = 114

    # Compound assignment such as "+=".
    COMPOUND_ASSIGNMENT_OPERATOR = 115

    # The ?: ternary operator.
    CONDITIONAL_OPERATOR = 116

    # An explicit cast in C (C99 6.5.4) or a C-style cast in C++
    # (C++ [expr.cast]), which uses the syntax (Type)expr.
    #
    # For example: (int)f.
    CSTYLE_CAST_EXPR = 117

    # [C99 6.5.2.5]
    COMPOUND_LITERAL_EXPR = 118

    # Describes an C or C++ initializer list.
    INIT_LIST_EXPR = 119

    # The GNU address of label extension, representing &&label.
    ADDR_LABEL_EXPR = 120

    # This is the GNU Statement Expression extension: ({int X=4; X;})
    StmtExpr = 121

    # Represents a C11 generic selection.
    GENERIC_SELECTION_EXPR = 122

    # Implements the GNU __null extension, which is a name for a null
    # pointer constant that has integral type (e.g., int or long) and is the
    # same size and alignment as a pointer.
    #
    # The __null extension is typically only used by system headers, which
    # define NULL as __null in C++ rather than using 0 (which is an integer that
    # may not match the size of a pointer).
    GNU_NULL_EXPR = 123

    # C++'s static_cast<> expression.
    CXX_STATIC_CAST_EXPR = 124

    # C++'s dynamic_cast<> expression.
    CXX_DYNAMIC_CAST_EXPR = 125

    # C++'s reinterpret_cast<> expression.
    CXX_REINTERPRET_CAST_EXPR = 126

    # C++'s const_cast<> expression.
    CXX_CONST_CAST_EXPR = 127

    # Represents an explicit C++ type conversion that uses "functional"
    # notion (C++ [expr.type.conv]).
    #
    # Example:
    # \code
    #   x = int(0.5);
    # \endcode
    CXX_FUNCTIONAL_CAST_EXPR = 128

    # A C++ typeid expression (C++ [expr.typeid]).
    CXX_TYPEID_EXPR = 129

    # [C++ 2.13.5] C++ Boolean Literal.
    CXX_BOOL_LITERAL_EXPR = 130

    # [C++0x 2.14.7] C++ Pointer Literal.
    CXX_NULL_PTR_LITERAL_EXPR = 131

    # Represents the "this" expression in C++
    CXX_THIS_EXPR = 132

    # [C++ 15] C++ Throw Expression.
    #
    # This handles 'throw' and 'throw' assignment-expression. When
    # assignment-expression isn't present, Op will be null.
    CXX_THROW_EXPR = 133

    # A new expression for memory allocation and constructor calls, e.g:
    # "new CXXNewExpr(foo)".
    CXX_NEW_EXPR = 134

    # A delete expression for memory deallocation and destructor calls,
    # e.g. "delete[] pArray".
    CXX_DELETE_EXPR = 135

    # Represents a unary expression.
    CXX_UNARY_EXPR = 136

    # ObjCStringLiteral, used for Objective-C string literals i.e. "foo".
    OBJC_STRING_LITERAL = 137

    # ObjCEncodeExpr, used for in Objective-C.
    OBJC_ENCODE_EXPR = 138

    # ObjCSelectorExpr used for in Objective-C.
    OBJC_SELECTOR_EXPR = 139

    # Objective-C's protocol expression.
    OBJC_PROTOCOL_EXPR = 140

    # An Objective-C "bridged" cast expression, which casts between Objective-C
    # pointers and C pointers, transferring ownership in the process.
    #
    # \code
    #   NSString *str = (__bridge_transfer NSString *)CFCreateString();
    # \endcode
    OBJC_BRIDGE_CAST_EXPR = 141

    # Represents a C++0x pack expansion that produces a sequence of
    # expressions.
    #
    # A pack expansion expression contains a pattern (which itself is an
    # expression) followed by an ellipsis. For example:
    PACK_EXPANSION_EXPR = 142

    # Represents an expression that computes the length of a parameter
    # pack.
    SIZE_OF_PACK_EXPR = 143

    # Represents a C++ lambda expression that produces a local function
    # object.
    #
    #  \code
    #  void abssort(float *x, unsigned N) {
    #    std::sort(x, x + N,
    #              [](float a, float b) {
    #                return std::abs(a) < std::abs(b);
    #              });
    #  }
    #  \endcode
    LAMBDA_EXPR = 144

    # Objective-c Boolean Literal.
    OBJ_BOOL_LITERAL_EXPR = 145

    # Represents the "self" expression in a ObjC method.
    OBJ_SELF_EXPR = 146

    # OpenMP 4.0 [2.4, Array Section].
    OMP_ARRAY_SECTION_EXPR = 147

    # Represents an @available(...) check.
    OBJC_AVAILABILITY_CHECK_EXPR = 148

    # Fixed point literal.
    FIXED_POINT_LITERAL = 149

    # OpenMP 5.0 [2.1.4, Array Shaping].
    OMP_ARRAY_SHAPING_EXPR = 150

    # OpenMP 5.0 [2.1.6 Iterators].
    OMP_ITERATOR_EXPR = 151

    # OpenCL's addrspace_cast<> expression.
    CXX_ADDRSPACE_CAST_EXPR = 152

    # Expression that references a C++20 concept.
    CONCEPT_SPECIALIZATION_EXPR = 153

    # Expression that references a C++20 requires expression.
    REQUIRES_EXPR = 154

    # Expression that references a C++20 parenthesized list aggregate
    # initializer.
    CXX_PAREN_LIST_INIT_EXPR = 155

    # Represents a C++26 pack indexing expression.
    PACK_INDEXING_EXPR = 156

    # A statement whose specific kind is not exposed via this interface.
    #
    # Unexposed statements have the same operations as any other kind of
    # statement; one can extract their location information, spelling, children,
    # etc. However, the specific kind of the statement is not reported.
    UNEXPOSED_STMT = 200

    # A labelled statement in a function.
    LABEL_STMT = 201

    # A compound statement
    COMPOUND_STMT = 202

    # A case statement.
    CASE_STMT = 203

    # A default statement.
    DEFAULT_STMT = 204

    # An if statement.
    IF_STMT = 205

    # A switch statement.
    SWITCH_STMT = 206

    # A while statement.
    WHILE_STMT = 207

    # A do statement.
    DO_STMT = 208

    # A for statement.
    FOR_STMT = 209

    # A goto statement.
    GOTO_STMT = 210

    # An indirect goto statement.
    INDIRECT_GOTO_STMT = 211

    # A continue statement.
    CONTINUE_STMT = 212

    # A break statement.
    BREAK_STMT = 213

    # A return statement.
    RETURN_STMT = 214

    # A GNU-style inline assembler statement.
    ASM_STMT = 215

    # Objective-C's overall @try-@catch-@finally statement.
    OBJC_AT_TRY_STMT = 216

    # Objective-C's @catch statement.
    OBJC_AT_CATCH_STMT = 217

    # Objective-C's @finally statement.
    OBJC_AT_FINALLY_STMT = 218

    # Objective-C's @throw statement.
    OBJC_AT_THROW_STMT = 219

    # Objective-C's @synchronized statement.
    OBJC_AT_SYNCHRONIZED_STMT = 220

    # Objective-C's autorelease pool statement.
    OBJC_AUTORELEASE_POOL_STMT = 221

    # Objective-C's for collection statement.
    OBJC_FOR_COLLECTION_STMT = 222

    # C++'s catch statement.
    CXX_CATCH_STMT = 223

    # C++'s try statement.
    CXX_TRY_STMT = 224

    # C++'s for (* : *) statement.
    CXX_FOR_RANGE_STMT = 225

    # Windows Structured Exception Handling's try statement.
    SEH_TRY_STMT = 226

    # Windows Structured Exception Handling's except statement.
    SEH_EXCEPT_STMT = 227

    # Windows Structured Exception Handling's finally statement.
    SEH_FINALLY_STMT = 228

    # A MS inline assembly statement extension.
    MS_ASM_STMT = 229

    # The null statement.
    NULL_STMT = 230

    # Adaptor class for mixing declarations with statements and expressions.
    DECL_STMT = 231

    # OpenMP parallel directive.
    OMP_PARALLEL_DIRECTIVE = 232

    # OpenMP SIMD directive.
    OMP_SIMD_DIRECTIVE = 233

    # OpenMP for directive.
    OMP_FOR_DIRECTIVE = 234

    # OpenMP sections directive.
    OMP_SECTIONS_DIRECTIVE = 235

    # OpenMP section directive.
    OMP_SECTION_DIRECTIVE = 236

    # OpenMP single directive.
    OMP_SINGLE_DIRECTIVE = 237

    # OpenMP parallel for directive.
    OMP_PARALLEL_FOR_DIRECTIVE = 238

    # OpenMP parallel sections directive.
    OMP_PARALLEL_SECTIONS_DIRECTIVE = 239

    # OpenMP task directive.
    OMP_TASK_DIRECTIVE = 240

    # OpenMP master directive.
    OMP_MASTER_DIRECTIVE = 241

    # OpenMP critical directive.
    OMP_CRITICAL_DIRECTIVE = 242

    # OpenMP taskyield directive.
    OMP_TASKYIELD_DIRECTIVE = 243

    # OpenMP barrier directive.
    OMP_BARRIER_DIRECTIVE = 244

    # OpenMP taskwait directive.
    OMP_TASKWAIT_DIRECTIVE = 245

    # OpenMP flush directive.
    OMP_FLUSH_DIRECTIVE = 246

    # Windows Structured Exception Handling's leave statement.
    SEH_LEAVE_STMT = 247

    # OpenMP ordered directive.
    OMP_ORDERED_DIRECTIVE = 248

    # OpenMP atomic directive.
    OMP_ATOMIC_DIRECTIVE = 249

    # OpenMP for SIMD directive.
    OMP_FOR_SIMD_DIRECTIVE = 250

    # OpenMP parallel for SIMD directive.
    OMP_PARALLELFORSIMD_DIRECTIVE = 251

    # OpenMP target directive.
    OMP_TARGET_DIRECTIVE = 252

    # OpenMP teams directive.
    OMP_TEAMS_DIRECTIVE = 253

    # OpenMP taskgroup directive.
    OMP_TASKGROUP_DIRECTIVE = 254

    # OpenMP cancellation point directive.
    OMP_CANCELLATION_POINT_DIRECTIVE = 255

    # OpenMP cancel directive.
    OMP_CANCEL_DIRECTIVE = 256

    # OpenMP target data directive.
    OMP_TARGET_DATA_DIRECTIVE = 257

    # OpenMP taskloop directive.
    OMP_TASK_LOOP_DIRECTIVE = 258

    # OpenMP taskloop simd directive.
    OMP_TASK_LOOP_SIMD_DIRECTIVE = 259

    # OpenMP distribute directive.
    OMP_DISTRIBUTE_DIRECTIVE = 260

    # OpenMP target enter data directive.
    OMP_TARGET_ENTER_DATA_DIRECTIVE = 261

    # OpenMP target exit data directive.
    OMP_TARGET_EXIT_DATA_DIRECTIVE = 262

    # OpenMP target parallel directive.
    OMP_TARGET_PARALLEL_DIRECTIVE = 263

    # OpenMP target parallel for directive.
    OMP_TARGET_PARALLELFOR_DIRECTIVE = 264

    # OpenMP target update directive.
    OMP_TARGET_UPDATE_DIRECTIVE = 265

    # OpenMP distribute parallel for directive.
    OMP_DISTRIBUTE_PARALLELFOR_DIRECTIVE = 266

    # OpenMP distribute parallel for simd directive.
    OMP_DISTRIBUTE_PARALLEL_FOR_SIMD_DIRECTIVE = 267

    # OpenMP distribute simd directive.
    OMP_DISTRIBUTE_SIMD_DIRECTIVE = 268

    # OpenMP target parallel for simd directive.
    OMP_TARGET_PARALLEL_FOR_SIMD_DIRECTIVE = 269

    # OpenMP target simd directive.
    OMP_TARGET_SIMD_DIRECTIVE = 270

    # OpenMP teams distribute directive.
    OMP_TEAMS_DISTRIBUTE_DIRECTIVE = 271

    # OpenMP teams distribute simd directive.
    OMP_TEAMS_DISTRIBUTE_SIMD_DIRECTIVE = 272

    # OpenMP teams distribute parallel for simd directive.
    OMP_TEAMS_DISTRIBUTE_PARALLEL_FOR_SIMD_DIRECTIVE = 273

    # OpenMP teams distribute parallel for directive.
    OMP_TEAMS_DISTRIBUTE_PARALLEL_FOR_DIRECTIVE = 274

    # OpenMP target teams directive.
    OMP_TARGET_TEAMS_DIRECTIVE = 275

    # OpenMP target teams distribute directive.
    OMP_TARGET_TEAMS_DISTRIBUTE_DIRECTIVE = 276

    # OpenMP target teams distribute parallel for directive.
    OMP_TARGET_TEAMS_DISTRIBUTE_PARALLEL_FOR_DIRECTIVE = 277

    # OpenMP target teams distribute parallel for simd directive.
    OMP_TARGET_TEAMS_DISTRIBUTE_PARALLEL_FOR_SIMD_DIRECTIVE = 278

    # OpenMP target teams distribute simd directive.
    OMP_TARGET_TEAMS_DISTRIBUTE_SIMD_DIRECTIVE = 279

    # C++2a std::bit_cast expression.
    BUILTIN_BIT_CAST_EXPR = 280

    # OpenMP master taskloop directive.
    OMP_MASTER_TASK_LOOP_DIRECTIVE = 281

    # OpenMP parallel master taskloop directive.
    OMP_PARALLEL_MASTER_TASK_LOOP_DIRECTIVE = 282

    # OpenMP master taskloop simd directive.
    OMP_MASTER_TASK_LOOP_SIMD_DIRECTIVE = 283

    # OpenMP parallel master taskloop simd directive.
    OMP_PARALLEL_MASTER_TASK_LOOP_SIMD_DIRECTIVE = 284

    # OpenMP parallel master directive.
    OMP_PARALLEL_MASTER_DIRECTIVE = 285

    # OpenMP depobj directive.
    OMP_DEPOBJ_DIRECTIVE = 286

    # OpenMP scan directive.
    OMP_SCAN_DIRECTIVE = 287

    # OpenMP tile directive.
    OMP_TILE_DIRECTIVE = 288

    # OpenMP canonical loop.
    OMP_CANONICAL_LOOP = 289

    # OpenMP interop directive.
    OMP_INTEROP_DIRECTIVE = 290

    # OpenMP dispatch directive.
    OMP_DISPATCH_DIRECTIVE = 291

    # OpenMP masked directive.
    OMP_MASKED_DIRECTIVE = 292

    # OpenMP unroll directive.
    OMP_UNROLL_DIRECTIVE = 293

    # OpenMP metadirective directive.
    OMP_META_DIRECTIVE = 294

    # OpenMP loop directive.
    OMP_GENERIC_LOOP_DIRECTIVE = 295

    # OpenMP teams loop directive.
    OMP_TEAMS_GENERIC_LOOP_DIRECTIVE = 296

    # OpenMP target teams loop directive.
    OMP_TARGET_TEAMS_GENERIC_LOOP_DIRECTIVE = 297

    # OpenMP parallel loop directive.
    OMP_PARALLEL_GENERIC_LOOP_DIRECTIVE = 298

    # OpenMP target parallel loop directive.
    OMP_TARGET_PARALLEL_GENERIC_LOOP_DIRECTIVE = 299

    # OpenMP parallel masked directive.
    OMP_PARALLEL_MASKED_DIRECTIVE = 300

    # OpenMP masked taskloop directive.
    OMP_MASKED_TASK_LOOP_DIRECTIVE = 301

    # OpenMP masked taskloop simd directive.
    OMP_MASKED_TASK_LOOP_SIMD_DIRECTIVE = 302

    # OpenMP parallel masked taskloop directive.
    OMP_PARALLEL_MASKED_TASK_LOOP_DIRECTIVE = 303

    # OpenMP parallel masked taskloop simd directive.
    OMP_PARALLEL_MASKED_TASK_LOOP_SIMD_DIRECTIVE = 304

    # OpenMP error directive.
    OMP_ERROR_DIRECTIVE = 305

    # OpenMP scope directive.
    OMP_SCOPE_DIRECTIVE = 306

    # OpenMP stripe directive.
    OMP_STRIPE_DIRECTIVE = 310

    # OpenACC Compute Construct.
    OPEN_ACC_COMPUTE_DIRECTIVE = 320

    ###
    # Other Kinds

    # Cursor that represents the translation unit itself.
    #
    # The translation unit cursor exists primarily to act as the root cursor for
    # traversing the contents of a translation unit.
    TRANSLATION_UNIT = 350

    ###
    # Attributes

    # An attribute whoe specific kind is note exposed via this interface
    UNEXPOSED_ATTR = 400

    IB_ACTION_ATTR = 401
    IB_OUTLET_ATTR = 402
    IB_OUTLET_COLLECTION_ATTR = 403

    CXX_FINAL_ATTR = 404
    CXX_OVERRIDE_ATTR = 405
    ANNOTATE_ATTR = 406
    ASM_LABEL_ATTR = 407
    PACKED_ATTR = 408
    PURE_ATTR = 409
    CONST_ATTR = 410
    NODUPLICATE_ATTR = 411
    CUDACONSTANT_ATTR = 412
    CUDADEVICE_ATTR = 413
    CUDAGLOBAL_ATTR = 414
    CUDAHOST_ATTR = 415
    CUDASHARED_ATTR = 416

    VISIBILITY_ATTR = 417

    DLLEXPORT_ATTR = 418
    DLLIMPORT_ATTR = 419
    NS_RETURNS_RETAINED = 420
    NS_RETURNS_NOT_RETAINED = 421
    NS_RETURNS_AUTORELEASED = 422
    NS_CONSUMES_SELF = 423
    NS_CONSUMED = 424
    OBJC_EXCEPTION = 425
    OBJC_NSOBJECT = 426
    OBJC_INDEPENDENT_CLASS = 427
    OBJC_PRECISE_LIFETIME = 428
    OBJC_RETURNS_INNER_POINTER = 429
    OBJC_REQUIRES_SUPER = 430
    OBJC_ROOT_CLASS = 431
    OBJC_SUBCLASSING_RESTRICTED = 432
    OBJC_EXPLICIT_PROTOCOL_IMPL = 433
    OBJC_DESIGNATED_INITIALIZER = 434
    OBJC_RUNTIME_VISIBLE = 435
    OBJC_BOXABLE = 436
    FLAG_ENUM = 437
    CONVERGENT_ATTR = 438
    WARN_UNUSED_ATTR = 439
    WARN_UNUSED_RESULT_ATTR = 440
    ALIGNED_ATTR = 441

    ###
    # Preprocessing
    PREPROCESSING_DIRECTIVE = 500
    MACRO_DEFINITION = 501
    MACRO_INSTANTIATION = 502
    INCLUSION_DIRECTIVE = 503

    ###
    # Extra declaration

    # A module import declaration.
    MODULE_IMPORT_DECL = 600
    # A type alias template declaration
    TYPE_ALIAS_TEMPLATE_DECL = 601
    # A static_assert or _Static_assert node
    STATIC_ASSERT = 602
    # A friend declaration
    FRIEND_DECL = 603
    # A concept declaration
    CONCEPT_DECL = 604

    # A code completion overload candidate.
    OVERLOAD_CANDIDATE = 700

### Template Argument Kinds ###
class TemplateArgumentKind(BaseEnumeration):
    """
    A TemplateArgumentKind describes the kind of entity that a template argument
    represents.
    """

    NULL = 0
    TYPE = 1
    DECLARATION = 2
    NULLPTR = 3
    INTEGRAL = 4
    TEMPLATE = 5
    TEMPLATE_EXPANSION = 6
    EXPRESSION = 7
    PACK = 8
    INVALID = 9

### Exception Specification Kinds ###
class ExceptionSpecificationKind(BaseEnumeration):
    """
    An ExceptionSpecificationKind describes the kind of exception specification
    that a function has.
    """

    NONE = 0
    DYNAMIC_NONE = 1
    DYNAMIC = 2
    MS_ANY = 3
    BASIC_NOEXCEPT = 4
    COMPUTED_NOEXCEPT = 5
    UNEVALUATED = 6
    UNINSTANTIATED = 7
    UNPARSED = 8

### Cursors ###


class Cursor(Structure):
    """
    The Cursor class represents a reference to an element within the AST. It
    acts as a kind of iterator.
    """

    _fields_ = [("_kind_id", c_int), ("xdata", c_int), ("data", c_void_p * 3)]

    @staticmethod
    def from_location(tu, location):
        # We store a reference to the TU in the instance so the TU won't get
        # collected before the cursor.
        cursor = conf.lib.clang_getCursor(tu, location)
        cursor._tu = tu

        return cursor

    def __eq__(self, other):
        if not isinstance(other, Cursor):
            return False
        return conf.lib.clang_equalCursors(self, other)  # type: ignore [no-any-return]

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return self.hash

    def is_definition(self):
        """
        Returns true if the declaration pointed at by the cursor is also a
        definition of that entity.
        """
        return conf.lib.clang_isCursorDefinition(self)  # type: ignore [no-any-return]

    def is_const_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared 'const'.
        """
        return conf.lib.clang_CXXMethod_isConst(self)  # type: ignore [no-any-return]

    def is_converting_constructor(self):
        """Returns True if the cursor refers to a C++ converting constructor."""
        return conf.lib.clang_CXXConstructor_isConvertingConstructor(self)  # type: ignore [no-any-return]

    def is_copy_constructor(self):
        """Returns True if the cursor refers to a C++ copy constructor."""
        return conf.lib.clang_CXXConstructor_isCopyConstructor(self)  # type: ignore [no-any-return]

    def is_default_constructor(self):
        """Returns True if the cursor refers to a C++ default constructor."""
        return conf.lib.clang_CXXConstructor_isDefaultConstructor(self)  # type: ignore [no-any-return]

    def is_move_constructor(self):
        """Returns True if the cursor refers to a C++ move constructor."""
        return conf.lib.clang_CXXConstructor_isMoveConstructor(self)  # type: ignore [no-any-return]

    def is_default_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared '= default'.
        """
        return conf.lib.clang_CXXMethod_isDefaulted(self)  # type: ignore [no-any-return]

    def is_deleted_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared '= delete'.
        """
        return conf.lib.clang_CXXMethod_isDeleted(self)  # type: ignore [no-any-return]

    def is_copy_assignment_operator_method(self):
        """Returnrs True if the cursor refers to a copy-assignment operator.

        A copy-assignment operator `X::operator=` is a non-static,
        non-template member function of _class_ `X` with exactly one
        parameter of type `X`, `X&`, `const X&`, `volatile X&` or `const
        volatile X&`.


        That is, for example, the `operator=` in:

           class Foo {
               bool operator=(const volatile Foo&);
           };

        Is a copy-assignment operator, while the `operator=` in:

           class Bar {
               bool operator=(const int&);
           };

        Is not.
        """
        return conf.lib.clang_CXXMethod_isCopyAssignmentOperator(self)  # type: ignore [no-any-return]

    def is_move_assignment_operator_method(self):
        """Returnrs True if the cursor refers to a move-assignment operator.

        A move-assignment operator `X::operator=` is a non-static,
        non-template member function of _class_ `X` with exactly one
        parameter of type `X&&`, `const X&&`, `volatile X&&` or `const
        volatile X&&`.


        That is, for example, the `operator=` in:

           class Foo {
               bool operator=(const volatile Foo&&);
           };

        Is a move-assignment operator, while the `operator=` in:

           class Bar {
               bool operator=(const int&&);
           };

        Is not.
        """
        return conf.lib.clang_CXXMethod_isMoveAssignmentOperator(self)  # type: ignore [no-any-return]

    def is_explicit_method(self):
        """Determines if a C++ constructor or conversion function is
        explicit, returning 1 if such is the case and 0 otherwise.

        Constructors or conversion functions are declared explicit through
        the use of the explicit specifier.

        For example, the following constructor and conversion function are
        not explicit as they lack the explicit specifier:

            class Foo {
                Foo();
                operator int();
            };

        While the following constructor and conversion function are
        explicit as they are declared with the explicit specifier.

            class Foo {
                explicit Foo();
                explicit operator int();
            };

        This method will return 0 when given a cursor pointing to one of
        the former declarations and it will return 1 for a cursor pointing
        to the latter declarations.

        The explicit specifier allows the user to specify a
        conditional compile-time expression whose value decides
        whether the marked element is explicit or not.

        For example:

            constexpr bool foo(int i) { return i % 2 == 0; }

            class Foo {
                 explicit(foo(1)) Foo();
                 explicit(foo(2)) operator int();
            }

        This method will return 0 for the constructor and 1 for
        the conversion function.
        """
        return conf.lib.clang_CXXMethod_isExplicit(self)  # type: ignore [no-any-return]

    def is_mutable_field(self):
        """Returns True if the cursor refers to a C++ field that is declared
        'mutable'.
        """
        return conf.lib.clang_CXXField_isMutable(self)  # type: ignore [no-any-return]

    def is_pure_virtual_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared pure virtual.
        """
        return conf.lib.clang_CXXMethod_isPureVirtual(self)  # type: ignore [no-any-return]

    def is_static_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared 'static'.
        """
        return conf.lib.clang_CXXMethod_isStatic(self)  # type: ignore [no-any-return]

    def is_virtual_method(self):
        """Returns True if the cursor refers to a C++ member function or member
        function template that is declared 'virtual'.
        """
        return conf.lib.clang_CXXMethod_isVirtual(self)  # type: ignore [no-any-return]

    def is_abstract_record(self):
        """Returns True if the cursor refers to a C++ record declaration
        that has pure virtual member functions.
        """
        return conf.lib.clang_CXXRecord_isAbstract(self)  # type: ignore [no-any-return]

    def is_scoped_enum(self):
        """Returns True if the cursor refers to a scoped enum declaration."""
        return conf.lib.clang_EnumDecl_isScoped(self)  # type: ignore [no-any-return]

    def get_definition(self):
        """
        If the cursor is a reference to a declaration or a declaration of
        some entity, return a cursor that points to the definition of that
        entity.
        """
        # TODO: Should probably check that this is either a reference or
        # declaration prior to issuing the lookup.
        return Cursor.from_result(conf.lib.clang_getCursorDefinition(self), self)

    def get_usr(self):
        """Return the Unified Symbol Resolution (USR) for the entity referenced
        by the given cursor.

        A Unified Symbol Resolution (USR) is a string that identifies a
        particular entity (function, class, variable, etc.) within a
        program. USRs can be compared across translation units to determine,
        e.g., when references in one translation refer to an entity defined in
        another translation unit."""
        return _CXString.from_result(conf.lib.clang_getCursorUSR(self))

    def get_included_file(self):
        """Returns the File that is included by the current inclusion cursor."""
        assert self.kind == CursorKind.INCLUSION_DIRECTIVE

        return File.from_result(conf.lib.clang_getIncludedFile(self), self)

    @property
    def kind(self):
        """Return the kind of this cursor."""
        return CursorKind.from_id(self._kind_id)

    @property
    def spelling(self):
        """Return the spelling of the entity pointed at by the cursor."""
        if not hasattr(self, "_spelling"):
            self._spelling = _CXString.from_result(
                conf.lib.clang_getCursorSpelling(self)
            )

        return self._spelling

    def pretty_printed(self, policy):
        """
        Pretty print declarations.
        Parameters:
        policy -- The policy to control the entities being printed.
        """
        return _CXString.from_result(
            conf.lib.clang_getCursorPrettyPrinted(self, policy)
        )

    @property
    def displayname(self):
        """
        Return the display name for the entity referenced by this cursor.

        The display name contains extra information that helps identify the
        cursor, such as the parameters of a function or template or the
        arguments of a class template specialization.
        """
        if not hasattr(self, "_displayname"):
            self._displayname = _CXString.from_result(
                conf.lib.clang_getCursorDisplayName(self)
            )

        return self._displayname

    @property
    def mangled_name(self):
        """Return the mangled name for the entity referenced by this cursor."""
        if not hasattr(self, "_mangled_name"):
            self._mangled_name = _CXString.from_result(
                conf.lib.clang_Cursor_getMangling(self)
            )

        return self._mangled_name

    @property
    def location(self):
        """
        Return the source location (the starting character) of the entity
        pointed at by the cursor.
        """
        if not hasattr(self, "_loc"):
            self._loc = conf.lib.clang_getCursorLocation(self)

        return self._loc

    @property
    def linkage(self):
        """Return the linkage of this cursor."""
        if not hasattr(self, "_linkage"):
            self._linkage = conf.lib.clang_getCursorLinkage(self)

        return LinkageKind.from_id(self._linkage)

    @property
    def tls_kind(self):
        """Return the thread-local storage (TLS) kind of this cursor."""
        if not hasattr(self, "_tls_kind"):
            self._tls_kind = conf.lib.clang_getCursorTLSKind(self)

        return TLSKind.from_id(self._tls_kind)

    @property
    def extent(self):
        """
        Return the source range (the range of text) occupied by the entity
        pointed at by the cursor.
        """
        if not hasattr(self, "_extent"):
            self._extent = conf.lib.clang_getCursorExtent(self)

        return self._extent

    @property
    def storage_class(self):
        """
        Retrieves the storage class (if any) of the entity pointed at by the
        cursor.
        """
        if not hasattr(self, "_storage_class"):
            self._storage_class = conf.lib.clang_Cursor_getStorageClass(self)

        return StorageClass.from_id(self._storage_class)

    @property
    def availability(self):
        """
        Retrieves the availability of the entity pointed at by the cursor.
        """
        if not hasattr(self, "_availability"):
            self._availability = conf.lib.clang_getCursorAvailability(self)

        return AvailabilityKind.from_id(self._availability)

    @property
    def binary_operator(self):
        """
        Retrieves the opcode if this cursor points to a binary operator
        :return:
        """

        if not hasattr(self, "_binopcode"):
            self._binopcode = conf.lib.clang_getCursorBinaryOperatorKind(self)

        return BinaryOperator.from_id(self._binopcode)

    @property
    def access_specifier(self):
        """
        Retrieves the access specifier (if any) of the entity pointed at by the
        cursor.
        """
        if not hasattr(self, "_access_specifier"):
            self._access_specifier = conf.lib.clang_getCXXAccessSpecifier(self)

        return AccessSpecifier.from_id(self._access_specifier)

    @property
    def type(self):
        """
        Retrieve the Type (if any) of the entity pointed at by the cursor.
        """
        if not hasattr(self, "_type"):
            self._type = Type.from_result(conf.lib.clang_getCursorType(self), (self,))

        return self._type

    @property
    def canonical(self):
        """Return the canonical Cursor corresponding to this Cursor.

        The canonical cursor is the cursor which is representative for the
        underlying entity. For example, if you have multiple forward
        declarations for the same class, the canonical cursor for the forward
        declarations will be identical.
        """
        if not hasattr(self, "_canonical"):
            self._canonical = Cursor.from_cursor_result(
                conf.lib.clang_getCanonicalCursor(self), self
            )

        return self._canonical

    @property
    def result_type(self):
        """Retrieve the Type of the result for this Cursor."""
        if not hasattr(self, "_result_type"):
            self._result_type = Type.from_result(
                conf.lib.clang_getCursorResultType(self), (self,)
            )

        return self._result_type

    @property
    def exception_specification_kind(self):
        """
        Retrieve the exception specification kind, which is one of the values
        from the ExceptionSpecificationKind enumeration.
        """
        if not hasattr(self, "_exception_specification_kind"):
            exc_kind = conf.lib.clang_getCursorExceptionSpecificationType(self)
            self._exception_specification_kind = ExceptionSpecificationKind.from_id(
                exc_kind
            )

        return self._exception_specification_kind

    @property
    def underlying_typedef_type(self):
        """Return the underlying type of a typedef declaration.

        Returns a Type for the typedef this cursor is a declaration for. If
        the current cursor is not a typedef, this raises.
        """
        if not hasattr(self, "_underlying_type"):
            assert self.kind.is_declaration()
            self._underlying_type = Type.from_result(
                conf.lib.clang_getTypedefDeclUnderlyingType(self), (self,)
            )

        return self._underlying_type

    @property
    def enum_type(self):
        """Return the integer type of an enum declaration.

        Returns a Type corresponding to an integer. If the cursor is not for an
        enum, this raises.
        """
        if not hasattr(self, "_enum_type"):
            assert self.kind == CursorKind.ENUM_DECL
            self._enum_type = Type.from_result(
                conf.lib.clang_getEnumDeclIntegerType(self), (self,)
            )

        return self._enum_type

    @property
    def enum_value(self):
        """Return the value of an enum constant."""
        if not hasattr(self, "_enum_value"):
            assert self.kind == CursorKind.ENUM_CONSTANT_DECL
            # Figure out the underlying type of the enum to know if it
            # is a signed or unsigned quantity.
            underlying_type = self.type
            if underlying_type.kind == TypeKind.ENUM:
                underlying_type = underlying_type.get_declaration().enum_type
            if underlying_type.kind in (
                TypeKind.CHAR_U,
                TypeKind.UCHAR,
                TypeKind.CHAR16,
                TypeKind.CHAR32,
                TypeKind.USHORT,
                TypeKind.UINT,
                TypeKind.ULONG,
                TypeKind.ULONGLONG,
                TypeKind.UINT128,
            ):
                self._enum_value = conf.lib.clang_getEnumConstantDeclUnsignedValue(self)
            else:
                self._enum_value = conf.lib.clang_getEnumConstantDeclValue(self)
        return self._enum_value

    @property
    def objc_type_encoding(self):
        """Return the Objective-C type encoding as a str."""
        if not hasattr(self, "_objc_type_encoding"):
            self._objc_type_encoding = _CXString.from_result(
                conf.lib.clang_getDeclObjCTypeEncoding(self)
            )

        return self._objc_type_encoding

    @property
    def hash(self):
        """Returns a hash of the cursor as an int."""
        if not hasattr(self, "_hash"):
            self._hash = conf.lib.clang_hashCursor(self)

        return self._hash

    @property
    def semantic_parent(self):
        """Return the semantic parent for this cursor."""
        if not hasattr(self, "_semantic_parent"):
            self._semantic_parent = Cursor.from_cursor_result(
                conf.lib.clang_getCursorSemanticParent(self), self
            )

        return self._semantic_parent

    @property
    def lexical_parent(self):
        """Return the lexical parent for this cursor."""
        if not hasattr(self, "_lexical_parent"):
            self._lexical_parent = Cursor.from_cursor_result(
                conf.lib.clang_getCursorLexicalParent(self), self
            )

        return self._lexical_parent

    @property
    def specialized_template(self) -> Cursor | None:
        """Return the primary template that this cursor is a specialization of, if any."""
        return Cursor.from_cursor_result(
            conf.lib.clang_getSpecializedCursorTemplate(self), self
        )

    @property
    def translation_unit(self):
        """Returns the TranslationUnit to which this Cursor belongs."""
        # If this triggers an AttributeError, the instance was not properly
        # created.
        return self._tu

    @property
    def referenced(self):
        """
        For a cursor that is a reference, returns a cursor
        representing the entity that it references.
        """
        if not hasattr(self, "_referenced"):
            self._referenced = Cursor.from_result(
                conf.lib.clang_getCursorReferenced(self), self
            )

        return self._referenced

    @property
    def brief_comment(self):
        """Returns the brief comment text associated with that Cursor"""
        return _CXString.from_result(conf.lib.clang_Cursor_getBriefCommentText(self))

    @property
    def raw_comment(self):
        """Returns the raw comment text associated with that Cursor"""
        return _CXString.from_result(conf.lib.clang_Cursor_getRawCommentText(self))

    def get_arguments(self):
        """Return an iterator for accessing the arguments of this cursor."""
        num_args = conf.lib.clang_Cursor_getNumArguments(self)
        for i in range(0, num_args):
            yield Cursor.from_result(conf.lib.clang_Cursor_getArgument(self, i), self)

    def get_num_template_arguments(self):
        """Returns the number of template args associated with this cursor."""
        return conf.lib.clang_Cursor_getNumTemplateArguments(self)  # type: ignore [no-any-return]

    def get_template_argument_kind(self, num):
        """Returns the TemplateArgumentKind for the indicated template
        argument."""
        return TemplateArgumentKind.from_id(
            conf.lib.clang_Cursor_getTemplateArgumentKind(self, num)
        )

    def get_template_argument_type(self, num):
        """Returns the CXType for the indicated template argument."""
        return Type.from_result(
            conf.lib.clang_Cursor_getTemplateArgumentType(self, num), (self, num)
        )

    def get_template_argument_value(self, num):
        """Returns the value of the indicated arg as a signed 64b integer."""
        return conf.lib.clang_Cursor_getTemplateArgumentValue(self, num)  # type: ignore [no-any-return]

    def get_template_argument_unsigned_value(self, num):
        """Returns the value of the indicated arg as an unsigned 64b integer."""
        return conf.lib.clang_Cursor_getTemplateArgumentUnsignedValue(self, num)  # type: ignore [no-any-return]

    def get_children(self):
        """Return an iterator for accessing the children of this cursor."""

        # FIXME: Expose iteration from CIndex, PR6125.
        def visitor(child, parent, children):
            # FIXME: Document this assertion in API.
            # FIXME: There should just be an isNull method.
            assert child != conf.lib.clang_getNullCursor()

            # Create reference to TU so it isn't GC'd before Cursor.
            child._tu = self._tu
            children.append(child)
            return 1  # continue

        children: list[Cursor] = []
        conf.lib.clang_visitChildren(self, cursor_visit_callback(visitor), children)
        return iter(children)

    def walk_preorder(self):
        """Depth-first preorder walk over the cursor and its descendants.

        Yields cursors.
        """
        yield self
        for child in self.get_children():
            for descendant in child.walk_preorder():
                yield descendant

    def get_tokens(self):
        """Obtain Token instances formulating that compose this Cursor.

        This is a generator for Token instances. It returns all tokens which
        occupy the extent this cursor occupies.
        """
        return TokenGroup.get_tokens(self._tu, self.extent)

    def get_field_offsetof(self):
        """Returns the offsetof the FIELD_DECL pointed by this Cursor."""
        return conf.lib.clang_Cursor_getOffsetOfField(self)  # type: ignore [no-any-return]

    def get_base_offsetof(self, parent):
        """Returns the offsetof the CXX_BASE_SPECIFIER pointed by this Cursor."""
        return conf.lib.clang_getOffsetOfBase(parent, self)  # type: ignore [no-any-return]

    def is_virtual_base(self):
        """Returns whether the CXX_BASE_SPECIFIER pointed by this Cursor is virtual."""
        return conf.lib.clang_isVirtualBase(self)  # type: ignore [no-any-return]

    def is_anonymous(self):
        """
        Check whether this is a record type without a name, or a field where
        the type is a record type without a name.

        Use is_anonymous_record_decl to check whether a record is an
        "anonymous union" as defined in the C/C++ standard.
        """
        if self.kind == CursorKind.FIELD_DECL:
            return self.type.get_declaration().is_anonymous()
        return conf.lib.clang_Cursor_isAnonymous(self)  # type: ignore [no-any-return]

    def is_anonymous_record_decl(self):
        """
        Check if the record is an anonymous union as defined in the C/C++ standard
        (or an "anonymous struct", the corresponding non-standard extension for
        structs).
        """
        if self.kind == CursorKind.FIELD_DECL:
            return self.type.get_declaration().is_anonymous_record_decl()
        return conf.lib.clang_Cursor_isAnonymousRecordDecl(self)  # type: ignore [no-any-return]

    def is_bitfield(self):
        """
        Check if the field is a bitfield.
        """
        return conf.lib.clang_Cursor_isBitField(self)  # type: ignore [no-any-return]

    def get_bitfield_width(self):
        """
        Retrieve the width of a bitfield.
        """
        return conf.lib.clang_getFieldDeclBitWidth(self)  # type: ignore [no-any-return]

    def has_attrs(self) -> bool:
        """
        Determine whether the given cursor has any attributes.
        """
        return bool(conf.lib.clang_Cursor_hasAttrs(self))

    @staticmethod
    def from_result(res, arg):
        assert isinstance(res, Cursor)
        # FIXME: There should just be an isNull method.
        if res == conf.lib.clang_getNullCursor():
            return None

        # Store a reference to the TU in the Python object so it won't get GC'd
        # before the Cursor.
        tu = None
        if isinstance(arg, TranslationUnit):
            tu = arg
        elif hasattr(arg, "translation_unit"):
            tu = arg.translation_unit

        assert tu is not None

        res._tu = tu
        return res

    @staticmethod
    def from_cursor_result(res, arg):
        assert isinstance(res, Cursor)
        if res == conf.lib.clang_getNullCursor():
            return None

        res._tu = arg._tu
        return res


class BinaryOperator(BaseEnumeration):
    """
    Describes the BinaryOperator of a declaration
    """

    def __nonzero__(self):
        """Allows checks of the kind ```if cursor.binary_operator:```"""
        return self.value != 0

    @property
    def is_assignment(self):
        return BinaryOperator.Assign.value <= self.value < BinaryOperator.Comma.value

    Invalid = 0
    PtrMemD = 1
    PtrMemI = 2
    Mul = 3
    Div = 4
    Rem = 5
    Add = 6
    Sub = 7
    Shl = 8
    Shr = 9
    Cmp = 10
    LT = 11
    GT = 12
    LE = 13
    GE = 14
    EQ = 15
    NE = 16
    And = 17
    Xor = 18
    Or = 19
    LAnd = 20
    LOr = 21
    Assign = 22
    MulAssign = 23
    DivAssign = 24
    RemAssign = 25
    AddAssign = 26
    SubAssign = 27
    ShlAssign = 28
    ShrAssign = 29
    AndAssign = 30
    XorAssign = 31
    OrAssign = 32
    Comma = 33


class StorageClass(BaseEnumeration):
    """
    Describes the storage class of a declaration
    """

    INVALID = 0
    NONE = 1
    EXTERN = 2
    STATIC = 3
    PRIVATEEXTERN = 4
    OPENCLWORKGROUPLOCAL = 5
    AUTO = 6
    REGISTER = 7

### Availability Kinds ###


class AvailabilityKind(BaseEnumeration):
    """
    Describes the availability of an entity.
    """

    AVAILABLE = 0
    DEPRECATED = 1
    NOT_AVAILABLE = 2
    NOT_ACCESSIBLE = 3

### C++ access specifiers ###


class AccessSpecifier(BaseEnumeration):
    """
    Describes the access of a C++ class member
    """

    INVALID = 0
    PUBLIC = 1
    PROTECTED = 2
    PRIVATE = 3
    NONE = 4

### Type Kinds ###


class TypeKind(BaseEnumeration):
    """
    Describes the kind of type.
    """

    @property
    def spelling(self):
        """Retrieve the spelling of this TypeKind."""
        return _CXString.from_result(conf.lib.clang_getTypeKindSpelling(self.value))

    INVALID = 0
    UNEXPOSED = 1
    VOID = 2
    BOOL = 3
    CHAR_U = 4
    UCHAR = 5
    CHAR16 = 6
    CHAR32 = 7
    USHORT = 8
    UINT = 9
    ULONG = 10
    ULONGLONG = 11
    UINT128 = 12
    CHAR_S = 13
    SCHAR = 14
    WCHAR = 15
    SHORT = 16
    INT = 17
    LONG = 18
    LONGLONG = 19
    INT128 = 20
    FLOAT = 21
    DOUBLE = 22
    LONGDOUBLE = 23
    NULLPTR = 24
    OVERLOAD = 25
    DEPENDENT = 26
    OBJCID = 27
    OBJCCLASS = 28
    OBJCSEL = 29
    FLOAT128 = 30
    HALF = 31
    IBM128 = 40
    COMPLEX = 100
    POINTER = 101
    BLOCKPOINTER = 102
    LVALUEREFERENCE = 103
    RVALUEREFERENCE = 104
    RECORD = 105
    ENUM = 106
    TYPEDEF = 107
    OBJCINTERFACE = 108
    OBJCOBJECTPOINTER = 109
    FUNCTIONNOPROTO = 110
    FUNCTIONPROTO = 111
    CONSTANTARRAY = 112
    VECTOR = 113
    INCOMPLETEARRAY = 114
    VARIABLEARRAY = 115
    DEPENDENTSIZEDARRAY = 116
    MEMBERPOINTER = 117
    AUTO = 118
    ELABORATED = 119
    PIPE = 120
    OCLIMAGE1DRO = 121
    OCLIMAGE1DARRAYRO = 122
    OCLIMAGE1DBUFFERRO = 123
    OCLIMAGE2DRO = 124
    OCLIMAGE2DARRAYRO = 125
    OCLIMAGE2DDEPTHRO = 126
    OCLIMAGE2DARRAYDEPTHRO = 127
    OCLIMAGE2DMSAARO = 128
    OCLIMAGE2DARRAYMSAARO = 129
    OCLIMAGE2DMSAADEPTHRO = 130
    OCLIMAGE2DARRAYMSAADEPTHRO = 131
    OCLIMAGE3DRO = 132
    OCLIMAGE1DWO = 133
    OCLIMAGE1DARRAYWO = 134
    OCLIMAGE1DBUFFERWO = 135
    OCLIMAGE2DWO = 136
    OCLIMAGE2DARRAYWO = 137
    OCLIMAGE2DDEPTHWO = 138
    OCLIMAGE2DARRAYDEPTHWO = 139
    OCLIMAGE2DMSAAWO = 140
    OCLIMAGE2DARRAYMSAAWO = 141
    OCLIMAGE2DMSAADEPTHWO = 142
    OCLIMAGE2DARRAYMSAADEPTHWO = 143
    OCLIMAGE3DWO = 144
    OCLIMAGE1DRW = 145
    OCLIMAGE1DARRAYRW = 146
    OCLIMAGE1DBUFFERRW = 147
    OCLIMAGE2DRW = 148
    OCLIMAGE2DARRAYRW = 149
    OCLIMAGE2DDEPTHRW = 150
    OCLIMAGE2DARRAYDEPTHRW = 151
    OCLIMAGE2DMSAARW = 152
    OCLIMAGE2DARRAYMSAARW = 153
    OCLIMAGE2DMSAADEPTHRW = 154
    OCLIMAGE2DARRAYMSAADEPTHRW = 155
    OCLIMAGE3DRW = 156
    OCLSAMPLER = 157
    OCLEVENT = 158
    OCLQUEUE = 159
    OCLRESERVEID = 160

    OBJCOBJECT = 161
    OBJCTYPEPARAM = 162
    ATTRIBUTED = 163

    OCLINTELSUBGROUPAVCMCEPAYLOAD = 164
    OCLINTELSUBGROUPAVCIMEPAYLOAD = 165
    OCLINTELSUBGROUPAVCREFPAYLOAD = 166
    OCLINTELSUBGROUPAVCSICPAYLOAD = 167
    OCLINTELSUBGROUPAVCMCERESULT = 168
    OCLINTELSUBGROUPAVCIMERESULT = 169
    OCLINTELSUBGROUPAVCREFRESULT = 170
    OCLINTELSUBGROUPAVCSICRESULT = 171
    OCLINTELSUBGROUPAVCIMERESULTSINGLEREFERENCESTREAMOUT = 172
    OCLINTELSUBGROUPAVCIMERESULTSDUALREFERENCESTREAMOUT = 173
    OCLINTELSUBGROUPAVCIMERESULTSSINGLEREFERENCESTREAMIN = 174
    OCLINTELSUBGROUPAVCIMEDUALREFERENCESTREAMIN = 175

    EXTVECTOR = 176
    ATOMIC = 177
    BTFTAGATTRIBUTED = 178

class RefQualifierKind(BaseEnumeration):
    """Describes a specific ref-qualifier of a type."""

    NONE = 0
    LVALUE = 1
    RVALUE = 2


class LinkageKind(BaseEnumeration):
    """Describes the kind of linkage of a cursor."""

    INVALID = 0
    NO_LINKAGE = 1
    INTERNAL = 2
    UNIQUE_EXTERNAL = 3
    EXTERNAL = 4


class TLSKind(BaseEnumeration):
    """Describes the kind of thread-local storage (TLS) of a cursor."""

    NONE = 0
    DYNAMIC = 1
    STATIC = 2


class Type(Structure):
    """
    The type of an element in the abstract syntax tree.
    """

    _fields_ = [("_kind_id", c_int), ("data", c_void_p * 2)]

    @property
    def kind(self):
        """Return the kind of this type."""
        return TypeKind.from_id(self._kind_id)

    def argument_types(self) -> NoSliceSequence[Type]:
        """Retrieve a container for the non-variadic arguments for this type.

        The returned object is iterable and indexable. Each item in the
        container is a Type instance.
        """

        class ArgumentsIterator:
            def __init__(self, parent: Type):
                self.parent = parent
                self.length: int | None = None

            def __len__(self) -> int:
                if self.length is None:
                    self.length = conf.lib.clang_getNumArgTypes(self.parent)

                return self.length

            def __getitem__(self, key: int) -> Type:
                # FIXME Support slice objects.
                if not isinstance(key, int):
                    raise TypeError("Must supply a non-negative int.")

                if key < 0:
                    raise IndexError("Only non-negative indexes are accepted.")

                if key >= len(self):
                    raise IndexError(
                        "Index greater than container length: "
                        "%d > %d" % (key, len(self))
                    )

                result = Type.from_result(
                    conf.lib.clang_getArgType(self.parent, key), (self.parent, key)
                )
                if result.kind == TypeKind.INVALID:
                    raise IndexError("Argument could not be retrieved.")

                return result

        assert self.kind == TypeKind.FUNCTIONPROTO
        return ArgumentsIterator(self)

    @property
    def element_type(self):
        """Retrieve the Type of elements within this Type.

        If accessed on a type that is not an array, complex, or vector type, an
        exception will be raised.
        """
        result = Type.from_result(conf.lib.clang_getElementType(self), (self,))
        if result.kind == TypeKind.INVALID:
            raise Exception("Element type not available on this type.")

        return result

    @property
    def element_count(self):
        """Retrieve the number of elements in this type.

        Returns an int.

        If the Type is not an array or vector, this raises.
        """
        result = conf.lib.clang_getNumElements(self)
        if result < 0:
            raise Exception("Type does not have elements.")

        return result

    @property
    def translation_unit(self):
        """The TranslationUnit to which this Type is associated."""
        # If this triggers an AttributeError, the instance was not properly
        # instantiated.
        return self._tu

    @staticmethod
    def from_result(res, args):
        assert isinstance(res, Type)

        tu = None
        for arg in args:
            if hasattr(arg, "translation_unit"):
                tu = arg.translation_unit
                break

        assert tu is not None
        res._tu = tu

        return res

    def get_num_template_arguments(self):
        return conf.lib.clang_Type_getNumTemplateArguments(self)  # type: ignore [no-any-return]

    def get_template_argument_type(self, num):
        return Type.from_result(
            conf.lib.clang_Type_getTemplateArgumentAsType(self, num), (self, num)
        )

    def get_canonical(self):
        """
        Return the canonical type for a Type.

        Clang's type system explicitly models typedefs and all the
        ways a specific type can be represented.  The canonical type
        is the underlying type with all the "sugar" removed.  For
        example, if 'T' is a typedef for 'int', the canonical type for
        'T' would be 'int'.
        """
        return Type.from_result(conf.lib.clang_getCanonicalType(self), (self,))

    def get_fully_qualified_name(self, policy, with_global_ns_prefix=False):
        """
        Get the fully qualified name for a type.

        This includes full qualification of all template parameters.

        policy - This PrintingPolicy can further refine the type formatting
        with_global_ns_prefix - If true, prepend '::' to qualified names
        """
        return _CXString.from_result(
            conf.lib.clang_getFullyQualifiedName(self, policy, with_global_ns_prefix)
        )

    def is_const_qualified(self):
        """Determine whether a Type has the "const" qualifier set.

        This does not look through typedefs that may have added "const"
        at a different level.
        """
        return conf.lib.clang_isConstQualifiedType(self)  # type: ignore [no-any-return]

    def is_volatile_qualified(self):
        """Determine whether a Type has the "volatile" qualifier set.

        This does not look through typedefs that may have added "volatile"
        at a different level.
        """
        return conf.lib.clang_isVolatileQualifiedType(self)  # type: ignore [no-any-return]

    def is_restrict_qualified(self):
        """Determine whether a Type has the "restrict" qualifier set.

        This does not look through typedefs that may have added "restrict" at
        a different level.
        """
        return conf.lib.clang_isRestrictQualifiedType(self)  # type: ignore [no-any-return]

    def is_function_variadic(self):
        """Determine whether this function Type is a variadic function type."""
        assert self.kind == TypeKind.FUNCTIONPROTO

        return conf.lib.clang_isFunctionTypeVariadic(self)  # type: ignore [no-any-return]

    def get_address_space(self):
        return conf.lib.clang_getAddressSpace(self)  # type: ignore [no-any-return]

    def get_typedef_name(self):
        return _CXString.from_result(conf.lib.clang_getTypedefName(self))

    def is_pod(self):
        """Determine whether this Type represents plain old data (POD)."""
        return conf.lib.clang_isPODType(self)  # type: ignore [no-any-return]

    def get_pointee(self):
        """
        For pointer types, returns the type of the pointee.
        """
        return Type.from_result(conf.lib.clang_getPointeeType(self), (self,))

    def get_declaration(self):
        """
        Return the cursor for the declaration of the given type.
        """
        return Cursor.from_result(conf.lib.clang_getTypeDeclaration(self), self)

    def get_result(self):
        """
        Retrieve the result type associated with a function type.
        """
        return Type.from_result(conf.lib.clang_getResultType(self), (self,))

    def get_array_element_type(self):
        """
        Retrieve the type of the elements of the array type.
        """
        return Type.from_result(conf.lib.clang_getArrayElementType(self), (self,))

    def get_array_size(self):
        """
        Retrieve the size of the constant array.
        """
        return conf.lib.clang_getArraySize(self)  # type: ignore [no-any-return]

    def get_class_type(self):
        """
        Retrieve the class type of the member pointer type.
        """
        return Type.from_result(conf.lib.clang_Type_getClassType(self), (self,))

    def get_named_type(self):
        """
        Retrieve the type named by the qualified-id.
        """
        return Type.from_result(conf.lib.clang_Type_getNamedType(self), (self,))

    def get_align(self):
        """
        Retrieve the alignment of the record.
        """
        return conf.lib.clang_Type_getAlignOf(self)  # type: ignore [no-any-return]

    def get_size(self):
        """
        Retrieve the size of the record.
        """
        return conf.lib.clang_Type_getSizeOf(self)  # type: ignore [no-any-return]

    def get_offset(self, fieldname):
        """
        Retrieve the offset of a field in the record.
        """
        return conf.lib.clang_Type_getOffsetOf(self, fieldname)  # type: ignore [no-any-return]

    def get_ref_qualifier(self):
        """
        Retrieve the ref-qualifier of the type.
        """
        return RefQualifierKind.from_id(conf.lib.clang_Type_getCXXRefQualifier(self))

    def get_fields(self):
        """Return an iterator for accessing the fields of this type."""

        def visitor(field, children):
            assert field != conf.lib.clang_getNullCursor()

            # Create reference to TU so it isn't GC'd before Cursor.
            field._tu = self._tu
            fields.append(field)
            return 1  # continue

        fields: list[Cursor] = []
        conf.lib.clang_Type_visitFields(self, fields_visit_callback(visitor), fields)
        return iter(fields)

    def get_bases(self):
        """Return an iterator for accessing the base classes of this type."""

        def visitor(base, children):
            assert base != conf.lib.clang_getNullCursor()

            # Create reference to TU so it isn't GC'd before Cursor.
            base._tu = self._tu
            bases.append(base)
            return 1  # continue

        bases: list[Cursor] = []
        conf.lib.clang_visitCXXBaseClasses(self, fields_visit_callback(visitor), bases)
        return iter(bases)

    def get_methods(self):
        """Return an iterator for accessing the methods of this type."""

        def visitor(method, children):
            assert method != conf.lib.clang_getNullCursor()

            # Create reference to TU so it isn't GC'd before Cursor.
            method._tu = self._tu
            methods.append(method)
            return 1  # continue

        methods: list[Cursor] = []
        conf.lib.clang_visitCXXMethods(self, fields_visit_callback(visitor), methods)
        return iter(methods)

    def get_exception_specification_kind(self):
        """
        Return the kind of the exception specification; a value from
        the ExceptionSpecificationKind enumeration.
        """
        return ExceptionSpecificationKind.from_id(
            conf.lib.clang_getExceptionSpecificationType(self)
        )

    @property
    def spelling(self):
        """Retrieve the spelling of this Type."""
        return _CXString.from_result(conf.lib.clang_getTypeSpelling(self))

    def pretty_printed(self, policy):
        """Pretty-prints this Type with the given PrintingPolicy"""
        return _CXString.from_result(conf.lib.clang_getTypePrettyPrinted(self, policy))

    def __eq__(self, other):
        if not isinstance(other, Type):
            return False

        return conf.lib.clang_equalTypes(self, other)  # type: ignore [no-any-return]

    def __ne__(self, other):
        return not self.__eq__(other)


## CIndex Objects ##

# CIndex objects (derived from ClangObject) are essentially lightweight
# wrappers attached to some underlying object, which is exposed via CIndex as
# a void*.


class ClangObject:
    """
    A helper for Clang objects. This class helps act as an intermediary for
    the ctypes library and the Clang CIndex library.
    """

    def __init__(self, obj):
        assert isinstance(obj, c_object_p) and obj
        self.obj = self._as_parameter_ = obj

    def from_param(self):
        return self._as_parameter_


class _CXUnsavedFile(Structure):
    """Helper for passing unsaved file arguments."""

    _fields_ = [("name", c_char_p), ("contents", c_char_p), ("length", c_ulong)]


# Functions calls through the python interface are rather slow. Fortunately,
# for most symboles, we do not need to perform a function call. Their spelling
# never changes and is consequently provided by this spelling cache.
SPELLING_CACHE = {
    # 0: CompletionChunk.Kind("Optional"),
    # 1: CompletionChunk.Kind("TypedText"),
    # 2: CompletionChunk.Kind("Text"),
    # 3: CompletionChunk.Kind("Placeholder"),
    # 4: CompletionChunk.Kind("Informative"),
    # 5 : CompletionChunk.Kind("CurrentParameter"),
    6: "(",  # CompletionChunk.Kind("LeftParen"),
    7: ")",  # CompletionChunk.Kind("RightParen"),
    8: "[",  # CompletionChunk.Kind("LeftBracket"),
    9: "]",  # CompletionChunk.Kind("RightBracket"),
    10: "{",  # CompletionChunk.Kind("LeftBrace"),
    11: "}",  # CompletionChunk.Kind("RightBrace"),
    12: "<",  # CompletionChunk.Kind("LeftAngle"),
    13: ">",  # CompletionChunk.Kind("RightAngle"),
    14: ", ",  # CompletionChunk.Kind("Comma"),
    # 15: CompletionChunk.Kind("ResultType"),
    16: ":",  # CompletionChunk.Kind("Colon"),
    17: ";",  # CompletionChunk.Kind("SemiColon"),
    18: "=",  # CompletionChunk.Kind("Equal"),
    19: " ",  # CompletionChunk.Kind("HorizontalSpace"),
    # 20: CompletionChunk.Kind("VerticalSpace")
}


class CompletionChunk:
    class Kind:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return self.name

        def __repr__(self):
            return "<ChunkKind: %s>" % self

    def __init__(self, completionString, key):
        self.cs = completionString
        self.key = key
        self.__kindNumberCache = -1

    def __repr__(self):
        return "{'" + self.spelling + "', " + str(self.kind) + "}"

    @CachedProperty
    def spelling(self):
        if self.__kindNumber in SPELLING_CACHE:
            return SPELLING_CACHE[self.__kindNumber]
        return _CXString.from_result(
            conf.lib.clang_getCompletionChunkText(self.cs, self.key)
        )

    # We do not use @CachedProperty here, as the manual implementation is
    # apparently still significantly faster. Please profile carefully if you
    # would like to add CachedProperty back.
    @property
    def __kindNumber(self):
        if self.__kindNumberCache == -1:
            self.__kindNumberCache = conf.lib.clang_getCompletionChunkKind(
                self.cs, self.key
            )
        return self.__kindNumberCache

    @CachedProperty
    def kind(self):
        return completionChunkKindMap[self.__kindNumber]

    @CachedProperty
    def string(self):
        res = conf.lib.clang_getCompletionChunkCompletionString(self.cs, self.key)

        if not res:
            return None
        return CompletionString(res)

    def isKindOptional(self):
        return self.__kindNumber == 0

    def isKindTypedText(self):
        return self.__kindNumber == 1

    def isKindPlaceHolder(self):
        return self.__kindNumber == 3

    def isKindInformative(self):
        return self.__kindNumber == 4

    def isKindResultType(self):
        return self.__kindNumber == 15


completionChunkKindMap = {
    0: CompletionChunk.Kind("Optional"),
    1: CompletionChunk.Kind("TypedText"),
    2: CompletionChunk.Kind("Text"),
    3: CompletionChunk.Kind("Placeholder"),
    4: CompletionChunk.Kind("Informative"),
    5: CompletionChunk.Kind("CurrentParameter"),
    6: CompletionChunk.Kind("LeftParen"),
    7: CompletionChunk.Kind("RightParen"),
    8: CompletionChunk.Kind("LeftBracket"),
    9: CompletionChunk.Kind("RightBracket"),
    10: CompletionChunk.Kind("LeftBrace"),
    11: CompletionChunk.Kind("RightBrace"),
    12: CompletionChunk.Kind("LeftAngle"),
    13: CompletionChunk.Kind("RightAngle"),
    14: CompletionChunk.Kind("Comma"),
    15: CompletionChunk.Kind("ResultType"),
    16: CompletionChunk.Kind("Colon"),
    17: CompletionChunk.Kind("SemiColon"),
    18: CompletionChunk.Kind("Equal"),
    19: CompletionChunk.Kind("HorizontalSpace"),
    20: CompletionChunk.Kind("VerticalSpace"),
}


class CompletionString(ClangObject):
    class Availability:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return self.name

        def __repr__(self):
            return "<Availability: %s>" % self

    def __len__(self):
        return self.num_chunks

    @CachedProperty
    def num_chunks(self):
        return conf.lib.clang_getNumCompletionChunks(self.obj)  # type: ignore [no-any-return]

    def __getitem__(self, key):
        if self.num_chunks <= key:
            raise IndexError
        return CompletionChunk(self.obj, key)

    if TYPE_CHECKING:
        # Defining __getitem__ and __len__ is enough to make an iterable
        # but the typechecker doesn't understand that.
        def __iter__(self):
            for i in range(len(self)):
                yield self[i]

    @property
    def priority(self):
        return conf.lib.clang_getCompletionPriority(self.obj)  # type: ignore [no-any-return]

    @property
    def availability(self):
        res = conf.lib.clang_getCompletionAvailability(self.obj)
        return availabilityKinds[res]

    @property
    def briefComment(self):
        return _CXString.from_result(conf.lib.clang_getCompletionBriefComment(self.obj))

    def __repr__(self):
        return (
            " | ".join([str(a) for a in self])
            + " || Priority: "
            + str(self.priority)
            + " || Availability: "
            + str(self.availability)
            + " || Brief comment: "
            + str(self.briefComment)
        )


availabilityKinds = {
    0: CompletionChunk.Kind("Available"),
    1: CompletionChunk.Kind("Deprecated"),
    2: CompletionChunk.Kind("NotAvailable"),
    3: CompletionChunk.Kind("NotAccessible"),
}


class CodeCompletionResult(Structure):
    _fields_ = [("cursorKind", c_int), ("completionString", c_object_p)]

    def __repr__(self):
        return str(CompletionString(self.completionString))

    @property
    def kind(self):
        return CursorKind.from_id(self.cursorKind)

    @property
    def string(self):
        return CompletionString(self.completionString)


class CCRStructure(Structure):
    _fields_ = [("results", POINTER(CodeCompletionResult)), ("numResults", c_int)]

    def __len__(self):
        return self.numResults

    def __getitem__(self, key):
        if len(self) <= key:
            raise IndexError

        return self.results[key]


class CodeCompletionResults(ClangObject):
    def __init__(self, ptr):
        assert isinstance(ptr, POINTER(CCRStructure)) and ptr
        self.ptr = self._as_parameter_ = ptr

    def from_param(self):
        return self._as_parameter_

    def __del__(self):
        conf.lib.clang_disposeCodeCompleteResults(self)

    @property
    def results(self):
        return self.ptr.contents

    @property
    def diagnostics(self) -> NoSliceSequence[Diagnostic]:
        class DiagnosticsItr:
            def __init__(self, ccr: CodeCompletionResults):
                self.ccr = ccr

            def __len__(self) -> int:
                return int(conf.lib.clang_codeCompleteGetNumDiagnostics(self.ccr))

            def __getitem__(self, key: int) -> Diagnostic:
                return conf.lib.clang_codeCompleteGetDiagnostic(self.ccr, key)  # type: ignore [no-any-return]

        return DiagnosticsItr(self)


class Index(ClangObject):
    """
    The Index type provides the primary interface to the Clang CIndex library,
    primarily by providing an interface for reading and parsing translation
    units.
    """

    @staticmethod
    def create(excludeDecls=False):
        """
        Create a new Index.
        Parameters:
        excludeDecls -- Exclude local declarations from translation units.
        """
        return Index(conf.lib.clang_createIndex(excludeDecls, 0))

    def __del__(self):
        conf.lib.clang_disposeIndex(self)

    def read(self, path):
        """Load a TranslationUnit from the given AST file."""
        return TranslationUnit.from_ast_file(path, self)

    def parse(self, path, args=None, unsaved_files=None, options=0):
        """Load the translation unit from the given source code file by running
        clang and generating the AST before loading. Additional command line
        parameters can be passed to clang via the args parameter.

        In-memory contents for files can be provided by passing a list of pairs
        to as unsaved_files, the first item should be the filenames to be mapped
        and the second should be the contents to be substituted for the
        file. The contents may be passed as strings or file objects.

        If an error was encountered during parsing, a TranslationUnitLoadError
        will be raised.
        """
        return TranslationUnit.from_source(path, args, unsaved_files, options, self)


class TranslationUnit(ClangObject):
    """Represents a source code translation unit.

    This is one of the main types in the API. Any time you wish to interact
    with Clang's representation of a source file, you typically start with a
    translation unit.
    """

    # Default parsing mode.
    PARSE_NONE = 0

    # Instruct the parser to create a detailed processing record containing
    # metadata not normally retained.
    PARSE_DETAILED_PROCESSING_RECORD = 1

    # Indicates that the translation unit is incomplete. This is typically used
    # when parsing headers.
    PARSE_INCOMPLETE = 2

    # Instruct the parser to create a pre-compiled preamble for the translation
    # unit. This caches the preamble (included files at top of source file).
    # This is useful if the translation unit will be reparsed and you don't
    # want to incur the overhead of reparsing the preamble.
    PARSE_PRECOMPILED_PREAMBLE = 4

    # Cache code completion information on parse. This adds time to parsing but
    # speeds up code completion.
    PARSE_CACHE_COMPLETION_RESULTS = 8

    # Flags with values 16 and 32 are deprecated and intentionally omitted.

    # Do not parse function bodies. This is useful if you only care about
    # searching for declarations/definitions.
    PARSE_SKIP_FUNCTION_BODIES = 64

    # Used to indicate that brief documentation comments should be included
    # into the set of code completions returned from this translation unit.
    PARSE_INCLUDE_BRIEF_COMMENTS_IN_CODE_COMPLETION = 128

    @staticmethod
    def process_unsaved_files(unsaved_files) -> Array[_CXUnsavedFile] | None:
        unsaved_array = None
        if len(unsaved_files):
            unsaved_array = (_CXUnsavedFile * len(unsaved_files))()
            for i, (name, contents) in enumerate(unsaved_files):
                if hasattr(contents, "read"):
                    contents = contents.read()
                binary_contents = b(contents)
                unsaved_array[i].name = b(os.fspath(name))
                unsaved_array[i].contents = binary_contents
                unsaved_array[i].length = len(binary_contents)
        return unsaved_array

    @classmethod
    def from_source(
        cls, filename, args=None, unsaved_files=None, options=0, index=None
    ):
        """Create a TranslationUnit by parsing source.

        This is capable of processing source code both from files on the
        filesystem as well as in-memory contents.

        Command-line arguments that would be passed to clang are specified as
        a list via args. These can be used to specify include paths, warnings,
        etc. e.g. ["-Wall", "-I/path/to/include"].

        In-memory file content can be provided via unsaved_files. This is a
        list of 2-tuples. The first element is the filename (str or
        PathLike). The second element defines the content. Content can be
        provided as str source code or as file objects (anything with a read()
        method). If a file object is being used, content will be read until EOF
        and the read cursor will not be reset to its original position.

        options is a bitwise or of TranslationUnit.PARSE_XXX flags which will
        control parsing behavior.

        index is an Index instance to utilize. If not provided, a new Index
        will be created for this TranslationUnit.

        To parse source from the filesystem, the filename of the file to parse
        is specified by the filename argument. Or, filename could be None and
        the args list would contain the filename(s) to parse.

        To parse source from an in-memory buffer, set filename to the virtual
        filename you wish to associate with this source (e.g. "test.c"). The
        contents of that file are then provided in unsaved_files.

        If an error occurs, a TranslationUnitLoadError is raised.

        Please note that a TranslationUnit with parser errors may be returned.
        It is the caller's responsibility to check tu.diagnostics for errors.

        Also note that Clang infers the source language from the extension of
        the input filename. If you pass in source code containing a C++ class
        declaration with the filename "test.c" parsing will fail.
        """
        if args is None:
            args = []

        if unsaved_files is None:
            unsaved_files = []

        if index is None:
            index = Index.create()

        args_array = None
        if len(args) > 0:
            args_array = (c_char_p * len(args))(*[b(x) for x in args])

        unsaved_array = cls.process_unsaved_files(unsaved_files)

        ptr = conf.lib.clang_parseTranslationUnit(
            index,
            os.fspath(filename) if filename is not None else None,
            args_array,
            len(args),
            unsaved_array,
            len(unsaved_files),
            options,
        )

        if not ptr:
            raise TranslationUnitLoadError("Error parsing translation unit.")

        return cls(ptr, index=index)

    @classmethod
    def from_ast_file(cls, filename, index=None):
        """Create a TranslationUnit instance from a saved AST file.

        A previously-saved AST file (provided with -emit-ast or
        TranslationUnit.save()) is loaded from the filename specified.

        If the file cannot be loaded, a TranslationUnitLoadError will be
        raised.

        index is optional and is the Index instance to use. If not provided,
        a default Index will be created.

        filename can be str or PathLike.
        """
        if index is None:
            index = Index.create()

        ptr = conf.lib.clang_createTranslationUnit(index, os.fspath(filename))
        if not ptr:
            raise TranslationUnitLoadError(filename)

        return cls(ptr=ptr, index=index)

    def __init__(self, ptr, index):
        """Create a TranslationUnit instance.

        TranslationUnits should be created using one of the from_* @classmethod
        functions above. __init__ is only called internally.
        """
        assert isinstance(index, Index)
        self.index = index
        ClangObject.__init__(self, ptr)

    def __del__(self):
        conf.lib.clang_disposeTranslationUnit(self)

    @property
    def cursor(self):
        """Retrieve the cursor that represents the given translation unit."""
        return Cursor.from_result(conf.lib.clang_getTranslationUnitCursor(self), self)

    @property
    def spelling(self):
        """Get the original translation unit source file name."""
        return _CXString.from_result(conf.lib.clang_getTranslationUnitSpelling(self))

    def get_includes(self):
        """
        Return an iterable sequence of FileInclusion objects that describe the
        sequence of inclusions in a translation unit. The first object in
        this sequence is always the input file. Note that this method will not
        recursively iterate over header files included through precompiled
        headers.
        """

        def visitor(fobj, lptr, depth, includes):
            if depth > 0:
                loc = lptr.contents
                includes.append(FileInclusion(loc.file, File(fobj), loc, depth))

        # Automatically adapt CIndex/ctype pointers to python objects
        includes = []
        conf.lib.clang_getInclusions(
            self, translation_unit_includes_callback(visitor), includes
        )

        return iter(includes)

    def get_file(self, filename):
        """Obtain a File from this translation unit."""

        return File.from_name(self, filename)

    def get_location(self, filename, position):
        """Obtain a SourceLocation for a file in this translation unit.

        The position can be specified by passing:

          - Integer file offset. Initial file offset is 0.
          - 2-tuple of (line number, column number). Initial file position is
            (0, 0)
        """
        f = self.get_file(filename)

        if isinstance(position, int):
            return SourceLocation.from_offset(self, f, position)

        return SourceLocation.from_position(self, f, position[0], position[1])

    def get_extent(self, filename, locations):
        """Obtain a SourceRange from this translation unit.

        The bounds of the SourceRange must ultimately be defined by a start and
        end SourceLocation. For the locations argument, you can pass:

          - 2 SourceLocation instances in a 2-tuple or list.
          - 2 int file offsets via a 2-tuple or list.
          - 2 2-tuple or lists of (line, column) pairs in a 2-tuple or list.

        e.g.

        get_extent('foo.c', (5, 10))
        get_extent('foo.c', ((1, 1), (1, 15)))
        """
        f = self.get_file(filename)

        if len(locations) < 2:
            raise Exception("Must pass object with at least 2 elements")

        start_location, end_location = locations

        if hasattr(start_location, "__len__"):
            start_location = Tcast(Sequence[int], start_location)
            start_location = SourceLocation.from_position(
                self, f, start_location[0], start_location[1]
            )
        elif isinstance(start_location, int):
            start_location = SourceLocation.from_offset(self, f, start_location)

        if hasattr(end_location, "__len__"):
            end_location = Tcast(Sequence[int], end_location)
            end_location = SourceLocation.from_position(
                self, f, end_location[0], end_location[1]
            )
        elif isinstance(end_location, int):
            end_location = SourceLocation.from_offset(self, f, end_location)

        assert isinstance(start_location, SourceLocation)
        assert isinstance(end_location, SourceLocation)

        return SourceRange.from_locations(start_location, end_location)

    @property
    def diagnostics(self) -> NoSliceSequence[Diagnostic]:
        """
        Return an iterable (and indexable) object containing the diagnostics.
        """

        class DiagIterator:
            def __init__(self, tu: TranslationUnit):
                self.tu = tu

            def __len__(self) -> int:
                return int(conf.lib.clang_getNumDiagnostics(self.tu))

            def __getitem__(self, key: int) -> Diagnostic:
                diag = conf.lib.clang_getDiagnostic(self.tu, key)
                if not diag:
                    raise IndexError
                return Diagnostic(diag)

        return DiagIterator(self)

    def reparse(self, unsaved_files=None, options=0):
        """
        Reparse an already parsed translation unit.

        In-memory contents for files can be provided by passing a list of pairs
        as unsaved_files, the first items should be the filenames to be mapped
        and the second should be the contents to be substituted for the
        file. The contents may be passed as strings or file objects.
        """
        if unsaved_files is None:
            unsaved_files = []

        unsaved_files_array = self.process_unsaved_files(unsaved_files)
        ptr = conf.lib.clang_reparseTranslationUnit(
            self, len(unsaved_files), unsaved_files_array, options
        )

    def save(self, filename):
        """Saves the TranslationUnit to a file.

        This is equivalent to passing -emit-ast to the clang frontend. The
        saved file can be loaded back into a TranslationUnit. Or, if it
        corresponds to a header, it can be used as a pre-compiled header file.

        If an error occurs while saving, a TranslationUnitSaveError is raised.
        If the error was TranslationUnitSaveError.ERROR_INVALID_TU, this means
        the constructed TranslationUnit was not valid at time of save. In this
        case, the reason(s) why should be available via
        TranslationUnit.diagnostics().

        filename -- The path to save the translation unit to (str or PathLike).
        """
        options = conf.lib.clang_defaultSaveOptions(self)
        result = int(
            conf.lib.clang_saveTranslationUnit(
                self,
                os.fspath(filename),
                options,
            )
        )
        if result != 0:
            raise TranslationUnitSaveError(result, "Error saving TranslationUnit.")

    def codeComplete(
        self,
        path,
        line,
        column,
        unsaved_files=None,
        include_macros=False,
        include_code_patterns=False,
        include_brief_comments=False,
    ):
        """
        Code complete in this translation unit.

        In-memory contents for files can be provided by passing a list of pairs
        as unsaved_files, the first items should be the filenames to be mapped
        and the second should be the contents to be substituted for the
        file. The contents may be passed as strings or file objects.
        """
        options = 0

        if include_macros:
            options += 1

        if include_code_patterns:
            options += 2

        if include_brief_comments:
            options += 4

        if unsaved_files is None:
            unsaved_files = []

        unsaved_files_array = self.process_unsaved_files(unsaved_files)
        ptr = conf.lib.clang_codeCompleteAt(
            self,
            os.fspath(path),
            line,
            column,
            unsaved_files_array,
            len(unsaved_files),
            options,
        )
        if ptr:
            return CodeCompletionResults(ptr)
        return None

    def get_tokens(self, locations=None, extent=None):
        """Obtain tokens in this translation unit.

        This is a generator for Token instances. The caller specifies a range
        of source code to obtain tokens for. The range can be specified as a
        2-tuple of SourceLocation or as a SourceRange. If both are defined,
        behavior is undefined.
        """
        if locations is None and extent is None:
            raise TypeError("get_tokens() requires at least one argument")
        if locations is not None:
            extent = SourceRange(start=locations[0], end=locations[1])

        return TokenGroup.get_tokens(self, extent)


class File(ClangObject):
    """
    The File class represents a particular source file that is part of a
    translation unit.
    """

    @staticmethod
    def from_name(translation_unit, file_name):
        """Retrieve a file handle within the given translation unit."""
        return File(
            conf.lib.clang_getFile(translation_unit, os.fspath(file_name)),
        )

    @property
    def name(self):
        """Return the complete file and path name of the file."""
        return _CXString.from_result(conf.lib.clang_getFileName(self))

    @property
    def time(self):
        """Return the last modification time of the file."""
        return conf.lib.clang_getFileTime(self)  # type: ignore [no-any-return]

    def __str__(self):
        return self.name

    def __repr__(self):
        return "<File: %s>" % (self.name)

    def __eq__(self, other) -> bool:
        return isinstance(other, File) and bool(
            conf.lib.clang_File_isEqual(self, other)
        )

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    @staticmethod
    def from_result(res, arg):
        assert isinstance(res, c_object_p)
        file = File(res)

        # Copy a reference to the TranslationUnit to prevent premature GC.
        file._tu = arg._tu
        return file


class FileInclusion:
    """
    The FileInclusion class represents the inclusion of one source file by
    another via a '#include' directive or as the input file for the translation
    unit. This class provides information about the included file, the including
    file, the location of the '#include' directive and the depth of the included
    file in the stack. Note that the input file has depth 0.
    """

    def __init__(self, src, tgt, loc, depth):
        self.source = src
        self.include = tgt
        self.location = loc
        self.depth = depth

    @property
    def is_input_file(self):
        """True if the included file is the input file."""
        return self.depth == 0


class CompilationDatabaseError(Exception):
    """Represents an error that occurred when working with a CompilationDatabase

    Each error is associated to an enumerated value, accessible under
    e.cdb_error. Consumers can compare the value with one of the ERROR_
    constants in this class.
    """

    # An unknown error occurred
    ERROR_UNKNOWN = 0

    # The database could not be loaded
    ERROR_CANNOTLOADDATABASE = 1

    def __init__(self, enumeration, message):
        assert isinstance(enumeration, int)

        if enumeration > 1:
            raise Exception(
                "Encountered undefined CompilationDatabase error "
                "constant: %d. Please file a bug to have this "
                "value supported." % enumeration
            )

        self.cdb_error = enumeration
        Exception.__init__(self, "Error %d: %s" % (enumeration, message))


class CompileCommand:
    """Represents the compile command used to build a file"""

    def __init__(self, cmd, ccmds):
        self.cmd = cmd
        # Keep a reference to the originating CompileCommands
        # to prevent garbage collection
        self.ccmds = ccmds

    @property
    def directory(self):
        """Get the working directory for this CompileCommand"""
        return _CXString.from_result(
            conf.lib.clang_CompileCommand_getDirectory(self.cmd)
        )

    @property
    def filename(self):
        """Get the working filename for this CompileCommand"""
        return _CXString.from_result(
            conf.lib.clang_CompileCommand_getFilename(self.cmd)
        )

    @property
    def arguments(self):
        """
        Get an iterable object providing each argument in the
        command line for the compiler invocation as a string.

        Invariant : the first argument is the compiler executable
        """
        length = conf.lib.clang_CompileCommand_getNumArgs(self.cmd)
        for i in range(length):
            yield _CXString.from_result(
                conf.lib.clang_CompileCommand_getArg(self.cmd, i)
            )


class CompileCommands:
    """
    CompileCommands is an iterable object containing all CompileCommand
    that can be used for building a specific file.
    """

    def __init__(self, ccmds):
        self.ccmds = ccmds

    def __del__(self):
        conf.lib.clang_CompileCommands_dispose(self.ccmds)

    def __len__(self):
        return int(conf.lib.clang_CompileCommands_getSize(self.ccmds))

    def __getitem__(self, i):
        cc = conf.lib.clang_CompileCommands_getCommand(self.ccmds, i)
        if not cc:
            raise IndexError
        return CompileCommand(cc, self)

    @staticmethod
    def from_result(res):
        if not res:
            return None
        return CompileCommands(res)


class CompilationDatabase(ClangObject):
    """
    The CompilationDatabase is a wrapper class around
    clang::tooling::CompilationDatabase

    It enables querying how a specific source file can be built.
    """

    def __del__(self):
        conf.lib.clang_CompilationDatabase_dispose(self)

    @staticmethod
    def from_result(res):
        if not res:
            raise CompilationDatabaseError(0, "CompilationDatabase loading failed")
        return CompilationDatabase(res)

    @staticmethod
    def fromDirectory(buildDir):
        """Builds a CompilationDatabase from the database found in buildDir"""
        errorCode = c_uint()
        try:
            cdb = CompilationDatabase.from_result(
                conf.lib.clang_CompilationDatabase_fromDirectory(
                    os.fspath(buildDir), byref(errorCode)
                )
            )
        except CompilationDatabaseError as e:
            raise CompilationDatabaseError(
                int(errorCode.value), "CompilationDatabase loading failed"
            )
        return cdb

    def getCompileCommands(self, filename):
        """
        Get an iterable object providing all the CompileCommands available to
        build filename. Returns None if filename is not found in the database.
        """
        return CompileCommands.from_result(
            conf.lib.clang_CompilationDatabase_getCompileCommands(  # type: ignore [no-any-return]
                self, os.fspath(filename)
            )
        )

    def getAllCompileCommands(self):
        """
        Get an iterable object providing all the CompileCommands available from
        the database.
        """
        return CompileCommands.from_result(
            conf.lib.clang_CompilationDatabase_getAllCompileCommands(self)  # type: ignore [no-any-return]
        )


class Token(Structure):
    """Represents a single token from the preprocessor.

    Tokens are effectively segments of source code. Source code is first parsed
    into tokens before being converted into the AST and Cursors.

    Tokens are obtained from parsed TranslationUnit instances. You currently
    can't create tokens manually.
    """

    _fields_ = [("int_data", c_uint * 4), ("ptr_data", c_void_p)]

    @property
    def spelling(self):
        """The spelling of this token.

        This is the textual representation of the token in source.
        """
        return _CXString.from_result(conf.lib.clang_getTokenSpelling(self._tu, self))

    @property
    def kind(self):
        """Obtain the TokenKind of the current token."""
        return TokenKind.from_value(conf.lib.clang_getTokenKind(self))

    @property
    def location(self):
        """The SourceLocation this Token occurs at."""
        return conf.lib.clang_getTokenLocation(self._tu, self)  # type: ignore [no-any-return]

    @property
    def extent(self):
        """The SourceRange this Token occupies."""
        return conf.lib.clang_getTokenExtent(self._tu, self)  # type: ignore [no-any-return]

    @property
    def cursor(self):
        """The Cursor this Token corresponds to."""
        cursor = Cursor()
        cursor._tu = self._tu

        conf.lib.clang_annotateTokens(self._tu, byref(self), 1, byref(cursor))

        return cursor


class Rewriter(ClangObject):
    """
    The Rewriter is a wrapper class around clang::Rewriter

    It enables rewriting buffers.
    """

    @staticmethod
    def create(tu):
        """
        Creates a new Rewriter
        Parameters:
        tu -- The translation unit for the target AST.
        """
        return Rewriter(conf.lib.clang_CXRewriter_create(tu))

    def __init__(self, ptr):
        ClangObject.__init__(self, ptr)

    def __del__(self):
        conf.lib.clang_CXRewriter_dispose(self)

    def insert_text_before(self, loc, insert):
        """
        Insert the specified string at the specified location in
        the original buffer.
        """
        conf.lib.clang_CXRewriter_insertTextBefore(self, loc, insert)

    def replace_text(self, extent, replacement):
        """
        This method replaces a range of characters in the input buffer with
        a new string.
        """
        conf.lib.clang_CXRewriter_replaceText(self, extent, replacement)

    def remove_text(self, extent):
        """
        Remove the specified text region.
        """
        conf.lib.clang_CXRewriter_removeText(self, extent)

    def overwrite_changed_files(self):
        """
        Save all changed files to disk.

        Returns 1 if any files were not saved successfully,
        returns 0 otherwise.
        """
        return conf.lib.clang_CXRewriter_overwriteChangedFiles(self)  # type: ignore [no-any-return]

    def write_main_file_to_stdout(self):
        """
        Writes the main file to stdout.
        """
        sys.stdout.flush()
        conf.lib.clang_CXRewriter_writeMainFileToStdOut(self)


class PrintingPolicyProperty(BaseEnumeration):

    """
    A PrintingPolicyProperty identifies a property of a PrintingPolicy.
    """

    Indentation = 0
    SuppressSpecifiers = 1
    SuppressTagKeyword = 2
    IncludeTagDefinition = 3
    SuppressScope = 4
    SuppressUnwrittenScope = 5
    SuppressInitializers = 6
    ConstantArraySizeAsWritten = 7
    AnonymousTagLocations = 8
    SuppressStrongLifetime = 9
    SuppressLifetimeQualifiers = 10
    SuppressTemplateArgsInCXXConstructors = 11
    Bool = 12
    Restrict = 13
    Alignof = 14
    UnderscoreAlignof = 15
    UseVoidForZeroParams = 16
    TerseOutput = 17
    PolishForDeclaration = 18
    Half = 19
    MSWChar = 20
    IncludeNewlines = 21
    MSVCFormatting = 22
    ConstantsAsWritten = 23
    SuppressImplicitBase = 24
    FullyQualifiedName = 25


class PrintingPolicy(ClangObject):
    """
    The PrintingPolicy is a wrapper class around clang::PrintingPolicy

    It allows specifying how declarations, expressions, and types should be
    pretty-printed.
    """

    @staticmethod
    def create(cursor):
        """
        Creates a new PrintingPolicy
        Parameters:
        cursor -- Any cursor for a translation unit.
        """
        return PrintingPolicy(conf.lib.clang_getCursorPrintingPolicy(cursor))

    def __init__(self, ptr):
        ClangObject.__init__(self, ptr)

    def __del__(self):
        conf.lib.clang_PrintingPolicy_dispose(self)

    def get_property(self, property):
        """Get a property value for the given printing policy."""
        return conf.lib.clang_PrintingPolicy_getProperty(self, property.value)

    def set_property(self, property, value):
        """Set a property value for the given printing policy."""
        conf.lib.clang_PrintingPolicy_setProperty(self, property.value, value)


# Now comes the plumbing to hook up the C library.

# Register callback types
translation_unit_includes_callback = CFUNCTYPE(
    None, c_object_p, POINTER(SourceLocation), c_uint, py_object
)
cursor_visit_callback = CFUNCTYPE(c_int, Cursor, Cursor, py_object)
fields_visit_callback = CFUNCTYPE(c_int, Cursor, py_object)

# Functions strictly alphabetical order.
FUNCTION_LIST: list[LibFunc] = [
    (
        "clang_annotateTokens",
        [TranslationUnit, POINTER(Token), c_uint, POINTER(Cursor)],
    ),
    ("clang_CompilationDatabase_dispose", [c_object_p]),
    (
        "clang_CompilationDatabase_fromDirectory",
        [c_interop_string, POINTER(c_uint)],
        c_object_p,
    ),
    ("clang_CompilationDatabase_getAllCompileCommands", [c_object_p], c_object_p),
    (
        "clang_CompilationDatabase_getCompileCommands",
        [c_object_p, c_interop_string],
        c_object_p,
    ),
    ("clang_CompileCommands_dispose", [c_object_p]),
    ("clang_CompileCommands_getCommand", [c_object_p, c_uint], c_object_p),
    ("clang_CompileCommands_getSize", [c_object_p], c_uint),
    ("clang_CompileCommand_getArg", [c_object_p, c_uint], _CXString),
    ("clang_CompileCommand_getDirectory", [c_object_p], _CXString),
    ("clang_CompileCommand_getFilename", [c_object_p], _CXString),
    ("clang_CompileCommand_getNumArgs", [c_object_p], c_uint),
    (
        "clang_codeCompleteAt",
        [TranslationUnit, c_interop_string, c_int, c_int, c_void_p, c_int, c_int],
        POINTER(CCRStructure),
    ),
    ("clang_codeCompleteGetDiagnostic", [CodeCompletionResults, c_int], Diagnostic),
    ("clang_codeCompleteGetNumDiagnostics", [CodeCompletionResults], c_int),
    ("clang_createIndex", [c_int, c_int], c_object_p),
    ("clang_createTranslationUnit", [Index, c_interop_string], c_object_p),
    ("clang_CXRewriter_create", [TranslationUnit], c_object_p),
    ("clang_CXRewriter_dispose", [Rewriter]),
    ("clang_CXRewriter_insertTextBefore", [Rewriter, SourceLocation, c_interop_string]),
    ("clang_CXRewriter_overwriteChangedFiles", [Rewriter], c_int),
    ("clang_CXRewriter_removeText", [Rewriter, SourceRange]),
    ("clang_CXRewriter_replaceText", [Rewriter, SourceRange, c_interop_string]),
    ("clang_CXRewriter_writeMainFileToStdOut", [Rewriter]),
    ("clang_CXXConstructor_isConvertingConstructor", [Cursor], bool),
    ("clang_CXXConstructor_isCopyConstructor", [Cursor], bool),
    ("clang_CXXConstructor_isDefaultConstructor", [Cursor], bool),
    ("clang_CXXConstructor_isMoveConstructor", [Cursor], bool),
    ("clang_CXXField_isMutable", [Cursor], bool),
    ("clang_CXXMethod_isConst", [Cursor], bool),
    ("clang_CXXMethod_isDefaulted", [Cursor], bool),
    ("clang_CXXMethod_isDeleted", [Cursor], bool),
    ("clang_CXXMethod_isCopyAssignmentOperator", [Cursor], bool),
    ("clang_CXXMethod_isMoveAssignmentOperator", [Cursor], bool),
    ("clang_CXXMethod_isExplicit", [Cursor], bool),
    ("clang_CXXMethod_isPureVirtual", [Cursor], bool),
    ("clang_CXXMethod_isStatic", [Cursor], bool),
    ("clang_CXXMethod_isVirtual", [Cursor], bool),
    ("clang_CXXRecord_isAbstract", [Cursor], bool),
    ("clang_EnumDecl_isScoped", [Cursor], bool),
    ("clang_defaultDiagnosticDisplayOptions", [], c_uint),
    ("clang_defaultSaveOptions", [TranslationUnit], c_uint),
    ("clang_disposeCodeCompleteResults", [CodeCompletionResults]),
    # ("clang_disposeCXTUResourceUsage",
    #  [CXTUResourceUsage]),
    ("clang_disposeDiagnostic", [Diagnostic]),
    ("clang_disposeIndex", [Index]),
    ("clang_disposeString", [_CXString]),
    ("clang_disposeTokens", [TranslationUnit, POINTER(Token), c_uint]),
    ("clang_disposeTranslationUnit", [TranslationUnit]),
    ("clang_equalCursors", [Cursor, Cursor], bool),
    ("clang_equalLocations", [SourceLocation, SourceLocation], bool),
    ("clang_equalRanges", [SourceRange, SourceRange], bool),
    ("clang_equalTypes", [Type, Type], bool),
    ("clang_formatDiagnostic", [Diagnostic, c_uint], _CXString),
    ("clang_getArgType", [Type, c_uint], Type),
    ("clang_getArrayElementType", [Type], Type),
    ("clang_getArraySize", [Type], c_longlong),
    ("clang_getFieldDeclBitWidth", [Cursor], c_int),
    ("clang_getCanonicalCursor", [Cursor], Cursor),
    ("clang_getCanonicalType", [Type], Type),
    ("clang_getChildDiagnostics", [Diagnostic], c_object_p),
    ("clang_getCompletionAvailability", [c_void_p], c_int),
    ("clang_getCompletionBriefComment", [c_void_p], _CXString),
    ("clang_getCompletionChunkCompletionString", [c_void_p, c_int], c_object_p),
    ("clang_getCompletionChunkKind", [c_void_p, c_int], c_int),
    ("clang_getCompletionChunkText", [c_void_p, c_int], _CXString),
    ("clang_getCompletionPriority", [c_void_p], c_int),
    ("clang_getCString", [_CXString], c_interop_string),
    ("clang_getCursor", [TranslationUnit, SourceLocation], Cursor),
    ("clang_getCursorAvailability", [Cursor], c_int),
    ("clang_getCursorDefinition", [Cursor], Cursor),
    ("clang_getCursorDisplayName", [Cursor], _CXString),
    ("clang_getCursorExtent", [Cursor], SourceRange),
    ("clang_getCursorLexicalParent", [Cursor], Cursor),
    ("clang_getCursorLocation", [Cursor], SourceLocation),
    ("clang_getCursorPrettyPrinted", [Cursor, PrintingPolicy], _CXString),
    ("clang_getCursorPrintingPolicy", [Cursor], c_object_p),
    ("clang_getCursorReferenced", [Cursor], Cursor),
    ("clang_getCursorReferenceNameRange", [Cursor, c_uint, c_uint], SourceRange),
    ("clang_getCursorResultType", [Cursor], Type),
    ("clang_getCursorSemanticParent", [Cursor], Cursor),
    ("clang_getCursorSpelling", [Cursor], _CXString),
    ("clang_getCursorType", [Cursor], Type),
    ("clang_getCursorUSR", [Cursor], _CXString),
    ("clang_Cursor_getMangling", [Cursor], _CXString),
    ("clang_Cursor_hasAttrs", [Cursor], c_uint),
    # ("clang_getCXTUResourceUsage",
    #  [TranslationUnit],
    #  CXTUResourceUsage),
    ("clang_getCXXAccessSpecifier", [Cursor], c_uint),
    ("clang_getDeclObjCTypeEncoding", [Cursor], _CXString),
    ("clang_getDiagnostic", [c_object_p, c_uint], c_object_p),
    ("clang_getDiagnosticCategory", [Diagnostic], c_uint),
    ("clang_getDiagnosticCategoryText", [Diagnostic], _CXString),
    ("clang_getDiagnosticFixIt", [Diagnostic, c_uint, POINTER(SourceRange)], _CXString),
    ("clang_getDiagnosticInSet", [c_object_p, c_uint], c_object_p),
    ("clang_getDiagnosticLocation", [Diagnostic], SourceLocation),
    ("clang_getDiagnosticNumFixIts", [Diagnostic], c_uint),
    ("clang_getDiagnosticNumRanges", [Diagnostic], c_uint),
    ("clang_getDiagnosticOption", [Diagnostic, POINTER(_CXString)], _CXString),
    ("clang_getDiagnosticRange", [Diagnostic, c_uint], SourceRange),
    ("clang_getDiagnosticSeverity", [Diagnostic], c_int),
    ("clang_getDiagnosticSpelling", [Diagnostic], _CXString),
    ("clang_getElementType", [Type], Type),
    ("clang_getEnumConstantDeclUnsignedValue", [Cursor], c_ulonglong),
    ("clang_getEnumConstantDeclValue", [Cursor], c_longlong),
    ("clang_getEnumDeclIntegerType", [Cursor], Type),
    ("clang_getFile", [TranslationUnit, c_interop_string], c_object_p),
    ("clang_getFileName", [File], _CXString),
    ("clang_getFileTime", [File], c_uint),
    ("clang_File_isEqual", [File, File], bool),
    ("clang_getIBOutletCollectionType", [Cursor], Type),
    ("clang_getIncludedFile", [Cursor], c_object_p),
    (
        "clang_getInclusions",
        [TranslationUnit, translation_unit_includes_callback, py_object],
    ),
    (
        "clang_getInstantiationLocation",
        [
            SourceLocation,
            POINTER(c_object_p),
            POINTER(c_uint),
            POINTER(c_uint),
            POINTER(c_uint),
        ],
    ),
    ("clang_getLocation", [TranslationUnit, File, c_uint, c_uint], SourceLocation),
    ("clang_getLocationForOffset", [TranslationUnit, File, c_uint], SourceLocation),
    ("clang_getNullCursor", None, Cursor),
    ("clang_getNumArgTypes", [Type], c_uint),
    ("clang_getNumCompletionChunks", [c_void_p], c_int),
    ("clang_getNumDiagnostics", [c_object_p], c_uint),
    ("clang_getNumDiagnosticsInSet", [c_object_p], c_uint),
    ("clang_getNumElements", [Type], c_longlong),
    ("clang_getNumOverloadedDecls", [Cursor], c_uint),
    ("clang_getOffsetOfBase", [Cursor, Cursor], c_longlong),
    ("clang_getOverloadedDecl", [Cursor, c_uint], Cursor),
    ("clang_getPointeeType", [Type], Type),
    ("clang_getRange", [SourceLocation, SourceLocation], SourceRange),
    ("clang_getRangeEnd", [SourceRange], SourceLocation),
    ("clang_getRangeStart", [SourceRange], SourceLocation),
    ("clang_getResultType", [Type], Type),
    ("clang_getSpecializedCursorTemplate", [Cursor], Cursor),
    ("clang_getTemplateCursorKind", [Cursor], c_uint),
    ("clang_getTokenExtent", [TranslationUnit, Token], SourceRange),
    ("clang_getTokenKind", [Token], c_uint),
    ("clang_getTokenLocation", [TranslationUnit, Token], SourceLocation),
    ("clang_getTokenSpelling", [TranslationUnit, Token], _CXString),
    ("clang_getTranslationUnitCursor", [TranslationUnit], Cursor),
    ("clang_getTranslationUnitSpelling", [TranslationUnit], _CXString),
    ("clang_getTUResourceUsageName", [c_uint], c_interop_string),
    ("clang_getTypeDeclaration", [Type], Cursor),
    ("clang_getTypedefDeclUnderlyingType", [Cursor], Type),
    ("clang_getTypedefName", [Type], _CXString),
    ("clang_getTypeKindSpelling", [c_uint], _CXString),
    ("clang_getTypePrettyPrinted", [Type, PrintingPolicy], _CXString),
    ("clang_getTypeSpelling", [Type], _CXString),
    ("clang_hashCursor", [Cursor], c_uint),
    ("clang_isAttribute", [CursorKind], bool),
    ("clang_getFullyQualifiedName", [Type, PrintingPolicy, c_uint], _CXString),
    ("clang_isConstQualifiedType", [Type], bool),
    ("clang_isCursorDefinition", [Cursor], bool),
    ("clang_isDeclaration", [CursorKind], bool),
    ("clang_isExpression", [CursorKind], bool),
    ("clang_isFileMultipleIncludeGuarded", [TranslationUnit, File], bool),
    ("clang_isFunctionTypeVariadic", [Type], bool),
    ("clang_isInvalid", [CursorKind], bool),
    ("clang_isPODType", [Type], bool),
    ("clang_isPreprocessing", [CursorKind], bool),
    ("clang_isReference", [CursorKind], bool),
    ("clang_isRestrictQualifiedType", [Type], bool),
    ("clang_isStatement", [CursorKind], bool),
    ("clang_isTranslationUnit", [CursorKind], bool),
    ("clang_isUnexposed", [CursorKind], bool),
    ("clang_isVirtualBase", [Cursor], bool),
    ("clang_isVolatileQualifiedType", [Type], bool),
    ("clang_isBeforeInTranslationUnit", [SourceLocation, SourceLocation], bool),
    (
        "clang_parseTranslationUnit",
        [Index, c_interop_string, c_void_p, c_int, c_void_p, c_int, c_int],
        c_object_p,
    ),
    ("clang_reparseTranslationUnit", [TranslationUnit, c_int, c_void_p, c_int], c_int),
    ("clang_saveTranslationUnit", [TranslationUnit, c_interop_string, c_uint], c_int),
    (
        "clang_tokenize",
        [TranslationUnit, SourceRange, POINTER(POINTER(Token)), POINTER(c_uint)],
    ),
    ("clang_visitChildren", [Cursor, cursor_visit_callback, py_object], c_uint),
    ("clang_visitCXXBaseClasses", [Type, fields_visit_callback, py_object], c_uint),
    ("clang_visitCXXMethods", [Type, fields_visit_callback, py_object], c_uint),
    ("clang_Cursor_getNumArguments", [Cursor], c_int),
    ("clang_Cursor_getArgument", [Cursor, c_uint], Cursor),
    ("clang_Cursor_getNumTemplateArguments", [Cursor], c_int),
    ("clang_Cursor_getTemplateArgumentKind", [Cursor, c_uint], c_uint),
    ("clang_Cursor_getTemplateArgumentType", [Cursor, c_uint], Type),
    ("clang_Cursor_getTemplateArgumentValue", [Cursor, c_uint], c_longlong),
    ("clang_Cursor_getTemplateArgumentUnsignedValue", [Cursor, c_uint], c_ulonglong),
    ("clang_getCursorBinaryOperatorKind", [Cursor], c_int),
    ("clang_Cursor_getBriefCommentText", [Cursor], _CXString),
    ("clang_Cursor_getRawCommentText", [Cursor], _CXString),
    ("clang_Cursor_getOffsetOfField", [Cursor], c_longlong),
    ("clang_Cursor_isAnonymous", [Cursor], bool),
    ("clang_Cursor_isAnonymousRecordDecl", [Cursor], bool),
    ("clang_Cursor_isBitField", [Cursor], bool),
    ("clang_Location_isInSystemHeader", [SourceLocation], bool),
    ("clang_PrintingPolicy_dispose", [PrintingPolicy]),
    ("clang_PrintingPolicy_getProperty", [PrintingPolicy, c_int], c_uint),
    ("clang_PrintingPolicy_setProperty", [PrintingPolicy, c_int, c_uint]),
    ("clang_Type_getAlignOf", [Type], c_longlong),
    ("clang_Type_getClassType", [Type], Type),
    ("clang_Type_getNumTemplateArguments", [Type], c_int),
    ("clang_Type_getTemplateArgumentAsType", [Type, c_uint], Type),
    ("clang_Type_getOffsetOf", [Type, c_interop_string], c_longlong),
    ("clang_Type_getSizeOf", [Type], c_longlong),
    ("clang_Type_getCXXRefQualifier", [Type], c_uint),
    ("clang_Type_getNamedType", [Type], Type),
    ("clang_Type_visitFields", [Type, fields_visit_callback, py_object], c_uint),
]


class LibclangError(Exception):
    def __init__(self, message: str):
        self.m = message

    def __str__(self) -> str:
        return self.m


def register_function(lib: CDLL, item: LibFunc, ignore_errors: bool) -> None:
    # A function may not exist, if these bindings are used with an older or
    # incompatible version of libclang.so.
    try:
        func = getattr(lib, item[0])
    except AttributeError as e:
        msg = (
            str(e) + ". Please ensure that your python bindings are "
            "compatible with your libclang.so version."
        )
        if ignore_errors:
            return
        raise LibclangError(msg)

    if len(item) >= 2:
        func.argtypes = item[1]

    if len(item) >= 3:
        func.restype = item[2]

    if len(item) == 4:
        func.errcheck = item[3]


def register_functions(lib: CDLL, ignore_errors: bool) -> None:
    """Register function prototypes with a libclang library instance.

    This must be called as part of library instantiation so Python knows how
    to call out to the shared library.
    """

    def register(item: LibFunc) -> None:
        register_function(lib, item, ignore_errors)

    for f in FUNCTION_LIST:
        register(f)


class Config:
    library_path = None
    library_file: str | None = None
    compatibility_check = True
    loaded = False

    @staticmethod
    def set_library_path(path: StrPath) -> None:
        """Set the path in which to search for libclang"""
        if Config.loaded:
            raise Exception(
                "library path must be set before before using "
                "any other functionalities in libclang."
            )

        Config.library_path = os.fspath(path)

    @staticmethod
    def set_library_file(filename: StrPath) -> None:
        """Set the exact location of libclang"""
        if Config.loaded:
            raise Exception(
                "library file must be set before before using "
                "any other functionalities in libclang."
            )

        Config.library_file = os.fspath(filename)

    @staticmethod
    def set_compatibility_check(check_status: bool) -> None:
        """Perform compatibility check when loading libclang

        The python bindings are only tested and evaluated with the version of
        libclang they are provided with. To ensure correct behavior a (limited)
        compatibility check is performed when loading the bindings. This check
        will throw an exception, as soon as it fails.

        In case these bindings are used with an older version of libclang, parts
        that have been stable between releases may still work. Users of the
        python bindings can disable the compatibility check. This will cause
        the python bindings to load, even though they are written for a newer
        version of libclang. Failures now arise if unsupported or incompatible
        features are accessed. The user is required to test themselves if the
        features they are using are available and compatible between different
        libclang versions.
        """
        if Config.loaded:
            raise Exception(
                "compatibility_check must be set before before "
                "using any other functionalities in libclang."
            )

        Config.compatibility_check = check_status

    @CachedProperty
    def lib(self) -> CDLL:
        lib = self.get_cindex_library()
        register_functions(lib, not Config.compatibility_check)
        Config.loaded = True
        return lib

    def get_filename(self) -> str:
        if Config.library_file:
            return Config.library_file

        import platform

        name = platform.system()

        if name == "Darwin":
            file = "libclang.dylib"
        elif name == "Windows":
            file = "libclang.dll"
        else:
            file = "libclang.so"

        if Config.library_path:
            file = Config.library_path + "/" + file

        return file

    def get_cindex_library(self) -> CDLL:
        try:
            library = cdll.LoadLibrary(self.get_filename())
        except OSError as e:
            msg = (
                str(e) + ". To provide a path to libclang use "
                "Config.set_library_path() or "
                "Config.set_library_file()."
            )
            raise LibclangError(msg)

        return library


conf = Config()

__all__ = [
    "AccessSpecifier",
    "AvailabilityKind",
    "BinaryOperator",
    "Config",
    "CodeCompletionResults",
    "CompilationDatabase",
    "CompileCommands",
    "CompileCommand",
    "CursorKind",
    "Cursor",
    "Diagnostic",
    "ExceptionSpecificationKind",
    "File",
    "FixIt",
    "Index",
    "LinkageKind",
    "PrintingPolicy",
    "PrintingPolicyProperty",
    "RefQualifierKind",
    "SourceLocation",
    "SourceRange",
    "StorageClass",
    "TemplateArgumentKind",
    "TLSKind",
    "TokenKind",
    "Token",
    "TranslationUnitLoadError",
    "TranslationUnit",
    "TypeKind",
    "Type",
]
