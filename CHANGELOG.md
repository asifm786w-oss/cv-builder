# Mulyba Change Log

## 2026-06-19

### Experience Section Stability Fix

Fixed a major state issue in the CV experience section.

AI-improved job roles were previously reverting or losing descriptions after improving another role. The section now keeps role descriptions stable across Streamlit reruns using role-level saved AI state.

Tested with a CV containing four roles during a 10+ minute session with no role loss, no data revert, and no disappearing descriptions.

Status: Resolved