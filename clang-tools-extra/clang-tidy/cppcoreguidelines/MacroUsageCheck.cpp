//===--- MacroUsageCheck.cpp - clang-tidy----------------------------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "MacroUsageCheck.h"
#include "clang/Basic/TokenKinds.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/Support/Regex.h"
#include <cctype>
#include <functional>

namespace clang::tidy::cppcoreguidelines {

static bool isCapsOnly(StringRef Name) {
  return llvm::all_of(Name, [](const char C) {
    return std::isupper(C) || std::isdigit(C) || C == '_';
  });
}

namespace {

class MacroUsageCallbacks : public PPCallbacks {
public:
  MacroUsageCallbacks(MacroUsageCheck *Check, const SourceManager &SM,
                      StringRef RegExpStr, bool CapsOnly,
                      bool IgnoreCommandLine)
      : Check(Check), SM(SM), RegExp(RegExpStr), CheckCapsOnly(CapsOnly),
        IgnoreCommandLineMacros(IgnoreCommandLine) {}
  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *MD) override {
    if (SM.isWrittenInBuiltinFile(MD->getLocation()) ||
        MD->getMacroInfo()->isUsedForHeaderGuard() ||
        MD->getMacroInfo()->tokens_empty() ||
        llvm::any_of(MD->getMacroInfo()->tokens(), [](const Token &T) {
          return T.isOneOf(tok::TokenKind::hash, tok::TokenKind::hashhash);
        }))
      return;

    if (IgnoreCommandLineMacros &&
        SM.isWrittenInCommandLineFile(MD->getLocation()))
      return;

    StringRef MacroName = MacroNameTok.getIdentifierInfo()->getName();
    if (MacroName == "__GCC_HAVE_DWARF2_CFI_ASM")
      return;
    if (!CheckCapsOnly && !RegExp.match(MacroName))
      Check->warnMacro(MD, MacroName);

    if (CheckCapsOnly && !isCapsOnly(MacroName))
      Check->warnNaming(MD, MacroName);
  }

private:
  MacroUsageCheck *Check;
  const SourceManager &SM;
  const llvm::Regex RegExp;
  bool CheckCapsOnly;
  bool IgnoreCommandLineMacros;
};
} // namespace

void MacroUsageCheck::storeOptions(ClangTidyOptions::OptionMap &Opts) {
  Options.store(Opts, "AllowedRegexp", AllowedRegexp);
  Options.store(Opts, "CheckCapsOnly", CheckCapsOnly);
  Options.store(Opts, "IgnoreCommandLineMacros", IgnoreCommandLineMacros);
}

void MacroUsageCheck::registerPPCallbacks(const SourceManager &SM,
                                          Preprocessor *PP,
                                          Preprocessor *ModuleExpanderPP) {
  PP->addPPCallbacks(std::make_unique<MacroUsageCallbacks>(
      this, SM, AllowedRegexp, CheckCapsOnly, IgnoreCommandLineMacros));
}

void MacroUsageCheck::warnMacro(const MacroDirective *MD, StringRef MacroName) {
  const MacroInfo *Info = MD->getMacroInfo();
  StringRef Message;

  if (llvm::all_of(Info->tokens(), std::mem_fn(&Token::isLiteral)))
    Message = "macro '%0' used to declare a constant; consider using a "
              "'constexpr' constant";
  // A variadic macro is function-like at the same time. Therefore variadic
  // macros are checked first and will be excluded for the function-like
  // diagnostic.
  else if (Info->isVariadic())
    Message = "variadic macro '%0' used; consider using a 'constexpr' "
              "variadic template function";
  else if (Info->isFunctionLike())
    Message = "function-like macro '%0' used; consider a 'constexpr' template "
              "function";

  if (!Message.empty())
    diag(MD->getLocation(), Message) << MacroName;
}

void MacroUsageCheck::warnNaming(const MacroDirective *MD,
                                 StringRef MacroName) {
  diag(MD->getLocation(), "macro definition does not define the macro name "
                          "'%0' using all uppercase characters")
      << MacroName;
}

} // namespace clang::tidy::cppcoreguidelines
