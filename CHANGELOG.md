# Mulyba Platform Features

## User Authentication & Account Management

* User registration and account creation
* Secure login and logout functionality
* Session management
* Persistent user accounts
* User profile storage
* Protected premium features
* Login-gated functionality
* User-specific usage tracking

---

## Access Control & Feature Protection

* Premium feature gate system
* Login requirement enforcement
* Credit-based access controls
* Cooldown protection against abuse
* Action locking for restricted features
* Usage validation before AI execution
* Protected workflow actions

---

## AI-Powered CV Builder

* CV creation wizard
* Structured CV generation
* Professional CV formatting
* Dynamic CV section management
* Multiple employment role support
* Skills section generation
* Education section management
* Personal profile generation
* Professional summary generation

---

## AI CV Enhancement Tools

* CV content improvement
* Professional wording enhancement
* Bullet point optimisation
* Experience description improvement
* Employment history enhancement
* Skills refinement
* Readability improvements
* ATS-friendly content enhancement
* Professional language transformation

---

## AI Job Role Improvement Engine

* Individual role enhancement
* Role-by-role AI optimisation
* Achievement-focused rewriting
* Professional responsibility expansion
* Career progression highlighting
* Experience section AI enhancement
* Persistent AI state management
* Multi-role editing support

---

## Cover Letter System

* AI-generated cover letters
* Tailored cover letter creation
* Professional cover letter formatting
* Job-specific content generation
* Editable cover letter outputs
* Cover letter enhancement tools

---

## CV Parsing & Upload System

* CV upload functionality
* PDF support
* DOCX support
* TXT support
* AI-powered CV extraction
* Automatic field population
* Employment history extraction
* Education extraction
* Skills extraction
* Personal information extraction
* Professional summary extraction

---

## Job Search Platform

* Live job vacancy search
* Adzuna integration
* Real-time vacancy retrieval
* Job discovery tools
* Career opportunity exploration
* Vacancy browsing
* Search filtering capabilities

---

## Credit System

* AI credit management
* CV credit management
* Credit consumption tracking
* Credit balance monitoring
* Credit spending controls
* Feature-based credit usage
* User credit allocation

---

## Referral Programme

* Referral tracking
* Referral rewards
* Free AI credit rewards
* Free CV credit rewards
* User growth incentives
* Community growth system

---

## Payments & Monetisation

* Stripe integration
* Online payment processing
* Credit package purchases
* Payment validation
* Subscription-ready infrastructure
* Revenue tracking foundation

---

## Usage Analytics

* Upload tracking
* AI usage monitoring
* Feature usage counters
* User activity tracking
* Platform engagement monitoring
* Service utilisation statistics

---

## Session Management & Data Persistence

* Session state management
* Persistent editing workflows
* Multi-step form support
* User workflow continuity
* State recovery mechanisms
* Safe rerun handling
* Data preservation during editing

---

## Education Management

* Education history builder
* Qualification tracking
* Academic record management
* Educational achievement storage
* Education state persistence

---

## Experience Management

* Multiple employment role support
* Dynamic role creation
* Role editing
* Employment history management
* Date tracking
* Location tracking
* Achievement tracking
* AI-enhanced experience editing
* Stable role persistence

---

## Platform Stability Features

* Cooldown protection
* Duplicate action prevention
* Error handling
* State recovery logic
* Data integrity safeguards
* Session reset controls
* Safe CV reprocessing
* Multi-role state persistence

---

## Cloud Infrastructure

* Railway deployment
* Cloud-hosted platform
* Online accessibility
* Database integration
* Production environment deployment
* GitHub source control
* Continuous development workflow

---

## Database Architecture

* PostgreSQL integration
* User data storage
* Credit storage
* Usage storage
* Platform analytics storage
* Persistent application data

---

## Future-Ready Foundations

* Scalable user management
* Expandable AI workflows
* Partner integration capability
* Educational institution collaboration readiness
* Employer engagement potential
* Community support platform foundation
* Future recruitment ecosystem expansion


# Historic Milestones

## The Beginning

Started with an idea to help people improve their CVs and job applications after experiencing first-hand how difficult the recruitment process can be.

Began learning Python programming with no traditional software development background while working full-time and studying.

---

## First Working Prototype

Built the first working version of Mulyba using Streamlit.

Created a basic CV generation workflow capable of collecting user information and producing structured CV content.

Successfully deployed the first version online.

---

## Learning the Technical Foundations

Learned:

* Python
* Streamlit
* APIs
* SQL
* PostgreSQL
* Git
* GitHub
* Cloud deployment
* Application debugging
* State management

Developed the confidence to build production features independently.

---

## OpenAI Integration

Integrated OpenAI into Mulyba.

Added AI-powered:

* CV improvements
* Cover letter generation
* Job role enhancement
* Content optimisation

Created workflows allowing users to improve existing content rather than starting from scratch.

---

## Database Implementation

Designed and implemented PostgreSQL database support.

Moved from temporary session-based data to persistent storage.

Created foundations for:

* User accounts
* Usage tracking
* Credit systems
* Platform analytics

---

## Stripe Integration

Successfully integrated Stripe payment processing.

Created paid packages allowing users to purchase platform credits.

Implemented:

* Payment processing
* Credit allocation
* Usage tracking
* Transaction handling

---

## Credit System

Designed and built the Mulyba credit model.

Introduced:

* AI credits
* CV credits
* Usage monitoring
* Credit consumption tracking

Provided a scalable commercial structure for future growth.

---

## Referral Programme

Developed referral functionality.

Users can earn:

* Free AI credits
* Free CV credits

through platform referrals.

Created an incentive model for organic growth.

---

## Adzuna Job Search Integration

Integrated Adzuna job search services.

Enabled users to:

* Search live vacancies
* Discover opportunities
* Connect CV improvement directly with active job searches

Moved Mulyba beyond CV creation into job discovery.

---

## Railway Migration

Migrated the application from Streamlit Cloud to Railway.

Improved:

* Reliability
* Scalability
* Deployment flexibility
* Performance

Created a stronger foundation for future development.

---

## User Authentication

Implemented account creation and login systems.

Added support for:

* User registration
* Authentication
* Session handling
* User-specific data management

---

## AI Role Improvement Engine

Built AI-assisted job role enhancement functionality.

Users can improve individual employment history entries using AI-generated enhancements.

Implemented safeguards including:

* Usage controls
* Credit consumption
* State persistence

---

## Experience Section Stability Project

Resolved a major state-management issue affecting experience history.

Problem:

AI improvements to one role could cause other role descriptions to revert or disappear.

Resolution:

Implemented dedicated role-level persistence handling.

Outcome:

* Stable multi-role editing
* Reliable rerun handling
* No role loss
* No description reversion

Successfully tested through extended editing sessions.

---

## Educational Journey

Returned to education while building Mulyba.

Applied learning from business, leadership and innovation studies directly into platform development.

Demonstrated practical application of enterprise and entrepreneurial thinking through real software development.

---

## Community Impact Vision

Mulyba evolved from a CV improvement tool into a wider community-focused employment support platform.

Mission:

To make career development, CV support and AI-powered job application assistance more accessible to everyone.

---

## Current Status

Active platform with:

* AI-powered CV support
* Cover letter generation
* Job role improvement
* Job search integration
* User accounts
* Credit system
* Referral programme
* Payment processing
* Cloud deployment
* PostgreSQL database

Continuing development and testing with future plans for partnerships, educational collaboration and platform growth.


# Mulyba Change Log

## 2026-06-19

### Experience Section Stability Fix

Fixed a major state issue in the CV experience section.

AI-improved job roles were previously reverting or losing descriptions after improving another role. The section now keeps role descriptions stable across Streamlit reruns using role-level saved AI state.

Tested with a CV containing four roles during a 10+ minute session with no role loss, no data revert, and no disappearing descriptions.

Status: Resolved